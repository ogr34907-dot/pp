"""Evidence-normalized LLM review for an outline direct-child cohort."""

from __future__ import annotations

import hashlib
import json
from typing import Any, Mapping

from application.ai.llm_json_extract import parse_llm_json_to_dict
from application.blueprint.services.outline_continuity_context_assembler import (
    OutlineContinuityContextAssembler,
)
from domain.ai.services.llm_service import GenerationConfig
from domain.structure.outline_continuity import (
    ContinuityDecision,
    ContinuityIssue,
    ContinuityIssueSeverity,
    ContinuityReviewReport,
    ContinuityReviewScope,
    build_scope_fingerprint,
)
from infrastructure.ai.generation_profiles import generation_config_from_profile
from infrastructure.ai.prompt_keys import OUTLINE_CONTINUITY_REVIEW
from infrastructure.ai.prompt_utils import render_required_prompt


SCHEMA_VERSION = "1"
RULESET_VERSION = "1"
MIN_CONFIDENCE = 0.65
FALLBACK_MODEL = "continuity-review-unresolved"


class OutlineContinuityReviewService:
    """Assemble, review, normalize, and persist one immutable report."""

    def __init__(
        self,
        repository: Any,
        review_repository: Any,
        llm_service: Any,
        db: Any,
        assembler: OutlineContinuityContextAssembler | None = None,
    ) -> None:
        self.repository = repository
        self.review_repository = review_repository
        self.llm_service = llm_service
        self.db = db
        self.assembler = assembler or OutlineContinuityContextAssembler(repository, db)

    async def review(
        self,
        plan_revision_id: str,
        parent_logical_node_id: str,
        expected_plan_digest: str,
        force: bool = False,
    ) -> dict[str, Any]:
        (
            context,
            scope,
            scope_fingerprint,
            config,
            prompts,
            setup_error,
            effective_model,
            prompt_version_id,
            prompt_hash,
            chunks,
        ) = self._prepare_review(
            plan_revision_id, parent_logical_node_id, expected_plan_digest
        )
        run = self.review_repository.begin(
            novel_id=context["novel_id"],
            plan_revision_id=plan_revision_id,
            scope=scope,
            plan_digest=context["plan_digest"],
            scope_fingerprint=scope_fingerprint,
            context_digest=context["context_digest"],
            prompt_node_version_id=prompt_version_id,
            prompt_hash=prompt_hash,
            schema_version=SCHEMA_VERSION,
            ruleset_version=RULESET_VERSION,
            model=effective_model,
            force=force,
        )
        if run["state"] != "running":
            return run
        if setup_error is not None:
            return self.review_repository.fail(
                run["id"], error=str(setup_error), raw_response=""
            )

        raw_outputs: list[str] = []
        try:
            reports = []
            for prompt, chunk in zip(prompts, chunks):
                response = await self.llm_service.generate(prompt, config)
                raw_outputs.append(str(response.content or ""))
                reports.append(
                    self._normalize(
                        raw_outputs[-1],
                        chunk,
                        scope_fingerprint=scope_fingerprint,
                        model=effective_model,
                    )
                )
            report = self._merge_reports(
                reports,
                scope_fingerprint=scope_fingerprint,
                model=effective_model,
            )
            raw_response = self._raw_response(raw_outputs, len(chunks))
            return self.review_repository.complete(
                run["id"], report=report, raw_response=raw_response
            )
        except Exception as exc:
            return self.review_repository.fail(
                run["id"],
                error=str(exc),
                raw_response=self._raw_response(raw_outputs, len(chunks)),
            )

    def current(
        self,
        plan_revision_id: str,
        parent_logical_node_id: str,
        expected_plan_digest: str,
    ) -> dict[str, Any] | None:
        """Resolve currentness from today's assembled scope, not the latest row."""

        context, _, scope_fingerprint, *_ = self._prepare_review(
            plan_revision_id, parent_logical_node_id, expected_plan_digest
        )
        return self.review_repository.current(
            plan_revision_id=context["plan_revision_id"],
            plan_digest=context["plan_digest"],
            scope_fingerprint=scope_fingerprint,
        )

    def current_scope_fingerprint(
        self,
        plan_revision_id: str,
        parent_logical_node_id: str,
        expected_plan_digest: str,
    ) -> str:
        """Return the fingerprint a fresh review would use for this scope."""

        _, _, scope_fingerprint, *_ = self._prepare_review(
            plan_revision_id, parent_logical_node_id, expected_plan_digest
        )
        return scope_fingerprint

    def required_current_reports(
        self, plan_revision_id: str, expected_plan_digest: str
    ) -> dict[str, dict[str, Any] | None]:
        """Resolve every changed cohort against the current Working snapshot."""

        plan = self.repository.get_plan_revision(plan_revision_id)
        if plan.digest != expected_plan_digest:
            raise ValueError("continuity review plan digest changed")
        return {
            parent_logical_node_id: self.current(
                plan_revision_id,
                parent_logical_node_id,
                expected_plan_digest,
            )
            for parent_logical_node_id in self.repository.required_narrative_review_scope_parents(
                plan_revision_id
            )
        }

    def _prepare_review(
        self,
        plan_revision_id: str,
        parent_logical_node_id: str,
        expected_plan_digest: str,
    ) -> tuple[
        dict[str, Any],
        ContinuityReviewScope,
        str,
        GenerationConfig,
        list[Any],
        Exception | None,
        str,
        str,
        str,
        list[Mapping[str, Any]],
    ]:
        context = self.assembler.assemble(plan_revision_id, parent_logical_node_id)
        if context["plan_digest"] != expected_plan_digest:
            raise ValueError("continuity review plan digest changed")
        setup_error: Exception | None = None
        try:
            config = generation_config_from_profile(
                "review_json", response_format={"type": "json_object"}
            )
        except Exception as exc:
            setup_error = exc
            config = GenerationConfig(
                model=FALLBACK_MODEL,
                max_tokens=1,
                temperature=0,
                response_format={"type": "json_object"},
            )
        prompts: list[Any] = []
        prompt_hash = ""
        prompt_version_id = ""
        chunks = context.get("review_chunks") or [context]
        if setup_error is None:
            try:
                for chunk in chunks:
                    prompts.append(
                        render_required_prompt(
                            OUTLINE_CONTINUITY_REVIEW,
                            {
                                "review_context": json.dumps(
                                    chunk, ensure_ascii=False, sort_keys=True
                                ),
                                "evidence_refs": json.dumps(
                                    chunk["evidence_refs"], ensure_ascii=False
                                ),
                                "ruleset_version": RULESET_VERSION,
                            },
                        )
                    )
                prompt_hash = self._digest(
                    [
                        {"system": prompt.system, "user": prompt.user}
                        for prompt in prompts
                    ]
                )
                prompt_version_id = self._prompt_version_id()
            except Exception as exc:
                setup_error = exc
        effective_model, generation_identity = self._generation_identity(config)
        scope = ContinuityReviewScope(**context["scope"])
        scope_fingerprint = build_scope_fingerprint(
            scope,
            ancestor_versions=context.get("ancestor_versions") or (),
            canonical_prefix_digest=context["canonical_prefix_digest"],
            context_digest=context["context_digest"],
            prompt_node_version_id=prompt_version_id,
            prompt_hash=prompt_hash,
            schema_version=SCHEMA_VERSION,
            ruleset_version=RULESET_VERSION,
            model=effective_model,
            context_metadata={
                "assembled_scope_fingerprint": context["scope_fingerprint"]
            },
            version_metadata={"generation": generation_identity},
        )
        context["scope_fingerprint"] = scope_fingerprint
        return (
            context,
            scope,
            scope_fingerprint,
            config,
            prompts,
            setup_error,
            effective_model,
            prompt_version_id,
            prompt_hash,
            chunks,
        )

    @classmethod
    def _normalize(
        cls,
        raw_response: str,
        context: Mapping[str, Any],
        *,
        scope_fingerprint: str | None = None,
        model: str = "",
    ) -> ContinuityReviewReport:
        data, errors = parse_llm_json_to_dict(raw_response)
        if not data:
            detail = "; ".join(errors) if errors else "empty JSON object"
            raise ValueError(f"continuity review returned invalid JSON: {detail}")
        decision = str(data.get("decision") or "").strip().lower()
        if decision not in {"pass", "review", "conflict"}:
            raise ValueError("continuity review decision must be pass, review, or conflict")
        confidence = data.get("confidence")
        if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
            raise ValueError("continuity review confidence must be a number")
        confidence = float(confidence)
        if not 0.0 <= confidence <= 1.0:
            raise ValueError("continuity review confidence must be between 0 and 1")
        raw_issues = data.get("issues")
        if not isinstance(raw_issues, list):
            raise ValueError("continuity review issues must be an array")
        raw_suggestions = data.get("suggestions", [])
        if not isinstance(raw_suggestions, list) or not all(
            isinstance(item, dict) for item in raw_suggestions
        ):
            raise ValueError("continuity review suggestions must be an object array")

        valid_refs = set(context["evidence_refs"])
        issues = tuple(cls._normalize_issue(item, valid_refs) for item in raw_issues)
        has_conflict = any(
            issue.severity is ContinuityIssueSeverity.CONFLICT
            and issue.evidence_refs
            for issue in issues
        )
        if has_conflict and confidence >= MIN_CONFIDENCE:
            final_decision = ContinuityDecision.CONFLICT
        elif any(
            issue.severity is ContinuityIssueSeverity.WARNING for issue in issues
        ) or confidence < MIN_CONFIDENCE:
            final_decision = ContinuityDecision.REVIEW
        else:
            final_decision = ContinuityDecision.PASS
        return ContinuityReviewReport(
            decision=final_decision,
            confidence=confidence,
            scope_fingerprint=scope_fingerprint or str(context["scope_fingerprint"]),
            issues=issues,
            suggestions=tuple(dict(item) for item in raw_suggestions),
            model=model,
            schema_version=SCHEMA_VERSION,
            ruleset_version=RULESET_VERSION,
        )

    @staticmethod
    def _normalize_issue(
        item: Any, valid_refs: set[str]
    ) -> ContinuityIssue:
        if not isinstance(item, dict):
            raise ValueError("continuity review issue must be an object")
        required = (
            "id",
            "code",
            "severity",
            "scope",
            "from_ref",
            "to_ref",
            "evidence_refs",
            "message",
            "suggestion",
            "suggested_patch",
        )
        if any(name not in item for name in required):
            raise ValueError("continuity review issue is missing required fields")
        for name in ("id", "code", "message"):
            if not isinstance(item[name], str) or not item[name].strip():
                raise ValueError(
                    f"continuity review issue {name} must be a non-empty string"
                )
        for name in ("from_ref", "to_ref", "suggestion"):
            if not isinstance(item[name], str):
                raise ValueError(f"continuity review issue {name} must be a string")
        if not isinstance(item["severity"], str):
            raise ValueError("continuity review issue severity must be a string")
        severity_text = item["severity"].strip().lower()
        if severity_text not in {"info", "warning", "conflict"}:
            raise ValueError("continuity review issue severity is invalid")
        if not isinstance(item["scope"], dict):
            raise ValueError("continuity review issue scope must be an object")
        if not isinstance(item["evidence_refs"], list) or not all(
            isinstance(value, str) for value in item["evidence_refs"]
        ):
            raise ValueError(
                "continuity review issue evidence_refs must be a string array"
            )
        if item["suggested_patch"] is not None and not isinstance(
            item["suggested_patch"], dict
        ):
            raise ValueError("continuity review issue suggested_patch must be an object")
        evidence_refs = tuple(
            ref for ref in item["evidence_refs"] if ref in valid_refs
        )
        severity = ContinuityIssueSeverity(severity_text)
        if severity is ContinuityIssueSeverity.CONFLICT and not evidence_refs:
            severity = ContinuityIssueSeverity.WARNING
        from_ref = item["from_ref"]
        to_ref = item["to_ref"]
        return ContinuityIssue(
            id=item["id"],
            code=item["code"],
            severity=severity,
            scope=dict(item["scope"]),
            from_ref=from_ref if from_ref in valid_refs else "",
            to_ref=to_ref if to_ref in valid_refs else "",
            evidence_refs=evidence_refs,
            message=item["message"],
            suggestion=item["suggestion"],
            suggested_patch=item["suggested_patch"],
        )

    def _generation_identity(self, config: GenerationConfig) -> tuple[str, dict[str, Any]]:
        requested_model = str(getattr(config, "model", "") or "")
        defaults = self._generation_defaults()
        effective_model = requested_model or str(defaults.get("model") or FALLBACK_MODEL)

        def effective(name: str, default_name: str | None = None) -> Any:
            value = getattr(config, name, None)
            is_explicit = getattr(config, "is_explicit", None)
            if callable(is_explicit) and not is_explicit(name):
                return defaults.get(default_name or name, value)
            return value

        identity = {
            "requested_model": requested_model,
            "effective_model": effective_model,
            "max_tokens": effective("max_tokens"),
            "temperature": effective("temperature"),
            "response_format": getattr(config, "response_format", None),
            "timeout_seconds": effective("timeout_seconds"),
            "reasoning_effort": getattr(config, "reasoning_effort", None),
            "thinking": getattr(config, "thinking", None),
        }
        return effective_model, identity

    def _generation_defaults(self) -> dict[str, Any]:
        factory = getattr(self.llm_service, "factory", None)
        control_service = getattr(factory, "control_service", None)
        if control_service is not None:
            try:
                profile = control_service.resolve_active_profile()
            except Exception:
                profile = None
            if profile is not None:
                return {
                    "model": getattr(profile, "model", ""),
                    "max_tokens": getattr(profile, "max_tokens", None),
                    "temperature": getattr(profile, "temperature", None),
                    "timeout_seconds": getattr(profile, "timeout_seconds", None),
                }
        candidates = (
            getattr(self.llm_service, "settings", None),
            getattr(getattr(self.llm_service, "provider", None), "settings", None),
            getattr(getattr(self.llm_service, "_cached_provider", None), "settings", None),
        )
        settings = next((item for item in candidates if item is not None), None)
        if settings is None:
            return {}
        return {
            "model": getattr(settings, "default_model", ""),
            "max_tokens": getattr(settings, "default_max_tokens", None),
            "temperature": getattr(settings, "default_temperature", None),
            "timeout_seconds": getattr(settings, "timeout_seconds", None),
        }

    @classmethod
    def _merge_reports(
        cls,
        reports: list[ContinuityReviewReport],
        *,
        scope_fingerprint: str,
        model: str,
    ) -> ContinuityReviewReport:
        severity_rank = {
            ContinuityIssueSeverity.INFO: 0,
            ContinuityIssueSeverity.WARNING: 1,
            ContinuityIssueSeverity.CONFLICT: 2,
        }
        issues: dict[str, ContinuityIssue] = {}
        suggestions: dict[str, Mapping[str, Any]] = {}
        for report in reports:
            for issue in report.issues:
                current = issues.get(issue.id)
                if current is None or severity_rank[issue.severity] > severity_rank[current.severity]:
                    issues[issue.id] = issue
            for suggestion in report.suggestions:
                key = json.dumps(
                    suggestion, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                )
                suggestions.setdefault(key, suggestion)
        merged_issues = tuple(issues.values())
        confidence = min(report.confidence for report in reports if report.confidence is not None)
        has_conflict = any(
            issue.severity is ContinuityIssueSeverity.CONFLICT and issue.evidence_refs
            for issue in merged_issues
        )
        if has_conflict and confidence >= MIN_CONFIDENCE:
            decision = ContinuityDecision.CONFLICT
        elif any(
            issue.severity is ContinuityIssueSeverity.WARNING for issue in merged_issues
        ) or confidence < MIN_CONFIDENCE:
            decision = ContinuityDecision.REVIEW
        else:
            decision = ContinuityDecision.PASS
        return ContinuityReviewReport(
            decision=decision,
            confidence=confidence,
            scope_fingerprint=scope_fingerprint,
            issues=merged_issues,
            suggestions=tuple(suggestions.values()),
            model=model,
            schema_version=SCHEMA_VERSION,
            ruleset_version=RULESET_VERSION,
        )

    @staticmethod
    def _raw_response(raw_outputs: list[str], chunk_count: int = 1) -> str:
        if not raw_outputs:
            return ""
        if chunk_count == 1:
            return raw_outputs[0]
        values = []
        for raw in raw_outputs:
            try:
                values.append(json.loads(raw))
            except json.JSONDecodeError:
                values.append(raw)
        return json.dumps(values, ensure_ascii=False, sort_keys=True)

    @staticmethod
    def _prompt_version_id() -> str:
        try:
            from infrastructure.ai.prompt_registry import get_prompt_registry

            node = get_prompt_registry().get_node(OUTLINE_CONTINUITY_REVIEW)
            return str(node.active_version_id or "") if node else ""
        except Exception:
            return ""

    @staticmethod
    def _digest(value: Any) -> str:
        encoded = json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
