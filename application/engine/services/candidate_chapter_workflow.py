"""Candidate-first chapter orchestration for continuous and review modes.

The service is intentionally the only bridge from an LLM draft to formal
chapter persistence.  Candidate prose and its audit are isolated until the
author (or the continuous-mode safety policy) accepts the commit plan.
"""

from __future__ import annotations

import hashlib
import inspect
import json
from typing import Any, Callable, Protocol

from application.audit.services.chapter_ai_review_service import normalize_evidence_text
from domain.novel.candidate_chapter import CandidateStatus, ChapterCandidate, GenerationRunState, RunMode
from infrastructure.persistence.database.chapter_candidate_repository import (
    CandidateGateError,
    ChapterCandidateRepository,
)


class CandidateWorkflowError(RuntimeError):
    """A recoverable candidate workflow failure already reflected in run state."""


class CandidateDraftGenerator(Protocol):
    async def generate_candidate_draft(self, **kwargs: Any) -> dict[str, Any]: ...


class CandidateAftermathPipeline(Protocol):
    async def run_after_chapter_saved(self, *args: Any, **kwargs: Any) -> dict[str, Any]: ...


class CandidateSemanticReviewer(Protocol):
    async def review(self, **kwargs: Any) -> Any: ...


class CandidateChapterWorkflowService:
    """Drive exactly one candidate at a time from published plan to formal sync."""

    def __init__(
        self,
        repository: ChapterCandidateRepository,
        outline_service: Any,
        draft_generator: CandidateDraftGenerator,
        aftermath_pipeline: CandidateAftermathPipeline,
        *,
        dag_engine: Any = None,
        dag_factory: Callable[[], Any] | None = None,
        semantic_reviewer: CandidateSemanticReviewer | None = None,
        max_candidate_revisions: int = 2,
    ) -> None:
        self.repository = repository
        self.outline_service = outline_service
        self.draft_generator = draft_generator
        self.aftermath_pipeline = aftermath_pipeline
        self.dag_engine = dag_engine
        self.dag_factory = dag_factory
        self.semantic_reviewer = semantic_reviewer
        self.max_candidate_revisions = max(0, int(max_candidate_revisions))

    async def generate_next(self, novel_id: str) -> ChapterCandidate | None:
        """Generate and audit only the next formal chapter slot.

        In review mode this ends in ``awaiting_review``.  In continuous mode
        only a soft-clean candidate reaches formal commit; hard plan conflicts
        always become an author-review stop rather than a hidden auto-commit.
        """

        run = self.repository.get_run(novel_id)
        if run.state != GenerationRunState.RUNNING:
            raise CandidateGateError(f"generation run is {run.state.value}; cannot generate next candidate")
        if run.current_candidate_id:
            raise CandidateGateError("pending candidate blocks next chapter generation")
        if run.current_formal_chapter >= run.target_chapters:
            self.repository.complete_run(novel_id)
            return None

        try:
            node, outline_chain = self.outline_service.next_published_chapter_context(
                novel_id, after_chapter=run.current_formal_chapter
            )
        except Exception as exc:
            self.repository.fail_run(novel_id, f"outline_chain_not_ready:{exc}")
            raise CandidateWorkflowError(f"published outline chain is not ready: {exc}") from exc

        expected_number = run.current_formal_chapter + 1
        if int(node.number) != expected_number:
            reason = f"next_outline_chapter_mismatch: expected {expected_number}, got {node.number}"
            self.repository.fail_run(novel_id, reason)
            raise CandidateWorkflowError(reason)
        chapter_payload = dict(outline_chain.get("chapter", {}).get("payload") or {})
        title = str(chapter_payload.get("title") or getattr(node, "title", "") or f"第{node.number}章")
        candidate = self.repository.create_streaming_candidate(
            novel_id=novel_id,
            chapter_number=int(node.number),
            title=title,
            outline_chain=outline_chain,
        )
        try:
            candidate, result = await self._generate_candidate_with_authority(candidate)
            self.repository.mark_auditing(candidate.id)
            audit, commit_plan = await self._audit_candidate(candidate, outline_chain, result)
            candidate = self.repository.finish_audit(
                candidate.id,
                audit=audit,
                commit_plan=commit_plan,
                require_author_review=bool(audit["hard_blocks"])
                or not bool(audit["required_events_complete"])
                or not bool(audit.get("dag", {}).get("approved", False)),
            )
        except Exception as exc:
            try:
                self.repository.fail_candidate(candidate.id, str(exc))
            except CandidateGateError:
                # A reset can retire a running task while the LLM is in flight;
                # the epoch guard is the desired terminal result in that case.
                pass
            raise CandidateWorkflowError(f"candidate generation failed: {exc}") from exc

        if run.run_mode == RunMode.CONTINUOUS and candidate.status == CandidateStatus.COMMITTING:
            return await self._commit_and_sync(candidate.id)
        return candidate

    async def accept_candidate(
        self, candidate_id: str, *, continue_after_commit: bool
    ) -> ChapterCandidate:
        """Apply the exact author-approved candidate revision and commit plan."""

        self.repository.approve_for_commit(
            candidate_id, continue_after_commit=continue_after_commit
        )
        return await self._commit_and_sync(candidate_id)

    async def regenerate_candidate(self, candidate_id: str, *, feedback: str = "") -> ChapterCandidate:
        """Regenerate and audit the same review candidate without formal effects.

        A regeneration request is not merely a status toggle: once the author
        asks for a new take, the candidate must receive new prose and a fresh
        audit before it can ever be committed.  The immutable version history
        remains in the repository and no formal chapter/fact is touched here.
        """

        candidate = self.repository.request_regeneration(candidate_id, feedback=feedback)
        try:
            candidate, result = await self._generate_candidate_with_authority(candidate)
            self.repository.mark_auditing(candidate.id)
            audit, commit_plan = await self._audit_candidate(candidate, candidate.outline_chain, result)
            # Manual regeneration always returns to review.  It may not
            # silently consume the author's approval by continuing a run.
            return self.repository.finish_audit(
                candidate.id,
                audit=audit,
                commit_plan=commit_plan,
                require_author_review=True,
            )
        except Exception as exc:
            try:
                self.repository.fail_candidate(candidate.id, str(exc))
            except CandidateGateError:
                pass
            raise CandidateWorkflowError(f"candidate regeneration failed: {exc}") from exc

    async def reaudit_candidate(self, candidate_id: str) -> ChapterCandidate:
        """Refresh audit and commit-plan data for an author-edited candidate.

        This is intentionally a zero-prose-generation operation.  It enables
        review desks to make the stale state explicit without silently spending
        another chapter's token budget.
        """

        candidate = self.repository.mark_auditing(candidate_id)
        try:
            audit, commit_plan = await self._audit_candidate(candidate, candidate.outline_chain, {})
            return self.repository.finish_audit(
                candidate.id,
                audit=audit,
                commit_plan=commit_plan,
                require_author_review=True,
            )
        except Exception as exc:
            try:
                self.repository.fail_candidate(candidate.id, str(exc))
            except CandidateGateError:
                pass
            raise CandidateWorkflowError(f"candidate re-audit failed: {exc}") from exc

    async def retry_canonical_sync(self, candidate_id: str) -> ChapterCandidate:
        """Retry only canonical aftermath; never generate or duplicate prose."""

        candidate = self.repository.begin_sync_retry(candidate_id)
        return await self._sync_formal_candidate(candidate)

    async def run_continuously(self, novel_id: str) -> list[ChapterCandidate]:
        """Advance until target, a hard gate, author review, or a recoverable error."""

        completed: list[ChapterCandidate] = []
        while True:
            run = self.repository.get_run(novel_id)
            if run.state != GenerationRunState.RUNNING:
                break
            if run.current_formal_chapter >= run.target_chapters:
                self.repository.complete_run(novel_id)
                break
            candidate = await self.generate_next(novel_id)
            if candidate is None:
                break
            completed.append(candidate)
            if candidate.status != CandidateStatus.COMMITTED:
                break
        return completed

    async def _commit_and_sync(self, candidate_id: str) -> ChapterCandidate:
        candidate = self.repository.commit_formal(candidate_id)
        return await self._sync_formal_candidate(candidate)

    async def _generate_candidate_with_authority(
        self, candidate: ChapterCandidate
    ) -> tuple[ChapterCandidate, dict[str, Any]]:
        """Run a bounded candidate revision loop through DAG V2 when available."""

        if self.dag_engine is None or self.dag_factory is None:
            candidate = self.repository.revalidate_candidate_generation_authority(
                candidate.id
            )
            from application.engine.dag.plan.schema import (
                chapter_rhythm_from_outline_payload,
                serialize_chapter_rhythm,
            )

            result = await self.draft_generator.generate_candidate_draft(
                novel_id=candidate.novel_id,
                chapter_number=candidate.chapter_number,
                chapter_title=candidate.title,
                outline_chain=candidate.outline_chain,
                outline_text=self._outline_text(candidate.outline_chain),
                chapter_rhythm=serialize_chapter_rhythm(
                    chapter_rhythm_from_outline_payload(
                        candidate.outline_chain.get("chapter", {}).get("payload", {})
                    )
                ),
            )
            content = self._draft_content(result)
            return self.repository.set_generated_content(candidate.id, content), dict(result)

        result: dict[str, Any] = {}
        for revision in range(self.max_candidate_revisions + 1):
            candidate = self.repository.revalidate_candidate_generation_authority(
                candidate.id
            )
            trace = self.repository.start_dag_run(
                candidate.id, content_revision=candidate.content_revision + 1
            )
            observer = _CandidateDAGObserver(self.repository, trace["id"])
            run_mode = self.repository.get_run(candidate.novel_id).run_mode.value
            initial_state = {
                "novel_id": candidate.novel_id,
                "chapter_number": candidate.chapter_number,
                "dag_run_id": trace["id"],
                "candidate_id": candidate.id,
                "candidate_revision": revision,
                "run_mode": run_mode,
                "outline": self._outline_text(candidate.outline_chain),
                "outline_chain": candidate.outline_chain,
                "retry_feedback": candidate.feedback,
                "disabled_nodes": ["plan_outline"],
                "node_configs": {},
            }
            runtime_context = {
                "candidate_draft_generator": self.draft_generator,
                "candidate_id": candidate.id,
                "candidate_revision": revision,
                "content_revision": candidate.content_revision + 1,
                "candidate_mode": True,
                "run_mode": run_mode,
                "outline_chain": candidate.outline_chain,
                "outline_text": self._outline_text(candidate.outline_chain),
                "chapter_title": candidate.title,
                "candidate_event_sink": observer.on_candidate_event,
                "candidate_semantic_reviewer": self.semantic_reviewer,
            }
            try:
                run_kwargs = {"thread_id": trace["id"]}
                try:
                    run_parameters = inspect.signature(self.dag_engine.run).parameters
                except (TypeError, ValueError):
                    run_parameters = {}
                if "observer" in run_parameters:
                    run_kwargs["observer"] = observer
                if "runtime_context" in run_parameters:
                    run_kwargs["runtime_context"] = runtime_context
                dag_result = await self.dag_engine.run(self.dag_factory(), initial_state, **run_kwargs)
                state = self._dag_result_state(dag_result)
                if dag_result.status != "completed":
                    self.repository.finish_dag_run(
                        trace["id"], status="failed", final_state=state, failure_reason="dag_execution_failed"
                    )
                    raise CandidateWorkflowError("candidate DAG execution failed")
                self.repository.finish_dag_run(trace["id"], status="completed", final_state=state)
            except Exception:
                if self._dag_run_is_running(trace["id"]):
                    self.repository.finish_dag_run(
                        trace["id"], status="failed", final_state={}, failure_reason="dag_execution_failed"
                    )
                raise
            content = self._draft_content(state)
            candidate = self.repository.set_generated_content(candidate.id, content)
            result = dict(state)
            if not bool(state.get("retry_requested")):
                return candidate, result
            if revision >= self.max_candidate_revisions:
                result["retry_exhausted"] = True
                return candidate, result
            candidate = self.repository.begin_dag_revision(
                candidate.id, feedback=str(state.get("retry_feedback") or "")
            )
        raise CandidateWorkflowError("candidate revision loop exhausted unexpectedly")

    def _dag_run_is_running(self, dag_run_id: str) -> bool:
        try:
            return self.repository.get_dag_run(dag_run_id)["status"] == "running"
        except KeyError:
            return False

    @staticmethod
    def _dag_result_state(dag_result: Any) -> dict[str, Any]:
        final_state = getattr(dag_result, "final_state", None)
        if isinstance(final_state, dict) and final_state:
            return dict(final_state)
        state: dict[str, Any] = {}
        for result in (getattr(dag_result, "node_results", {}) or {}).values():
            outputs = getattr(result, "outputs", result)
            if isinstance(outputs, dict):
                state.update(outputs)
        return state

    async def _sync_formal_candidate(self, candidate: ChapterCandidate) -> ChapterCandidate:
        content_sha256 = hashlib.sha256(candidate.final_content.encode("utf-8")).hexdigest()
        try:
            result = self.aftermath_pipeline.run_after_chapter_saved(
                candidate.novel_id,
                candidate.chapter_number,
                candidate.final_content,
                expected_content_sha256=content_sha256,
                expected_content_revision=candidate.content_revision,
                outline=self._commit_plan_text(candidate.commit_plan),
            )
            if inspect.isawaitable(result):
                result = await result
            if not isinstance(result, dict) or not result.get("narrative_sync_ok"):
                reason = "canonical_aftermath_not_ready"
                if isinstance(result, dict) and result.get("failure_reason"):
                    reason = str(result["failure_reason"])
                return self.repository.mark_sync_failed(candidate.id, reason)
        except Exception as exc:
            return self.repository.mark_sync_failed(candidate.id, str(exc))
        return self.repository.mark_sync_succeeded(candidate.id)

    @staticmethod
    def _outline_text(outline_chain: dict[str, Any]) -> str:
        """Use only synced plan projections in the LLM prompt, never draft/history."""

        return json.dumps(outline_chain, ensure_ascii=False, sort_keys=True)

    @staticmethod
    def _commit_plan_text(commit_plan: dict[str, Any]) -> str:
        """Render the author-approved plan in the stable order consumed by Canonical sync."""

        lines: list[str] = []
        for key in ("chapter_summary", "summary", "timeline_events", "next_chapter_handoff"):
            value = commit_plan.get(key)
            if isinstance(value, (list, tuple)):
                lines.extend(str(item).strip() for item in value if str(item).strip())
            elif value is not None and str(value).strip():
                lines.append(str(value).strip())
        return "\n".join(lines)

    @staticmethod
    def _draft_content(result: Any) -> str:
        if isinstance(result, str):
            content = result
        elif isinstance(result, dict):
            content = str(result.get("content") or result.get("prose") or "")
        else:
            content = str(getattr(result, "content", "") or "")
        if not content.strip():
            raise CandidateWorkflowError("candidate generator returned empty prose")
        return content

    async def _audit_candidate(
        self,
        candidate: ChapterCandidate,
        outline_chain: dict[str, Any],
        generation_result: Any,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """Build a no-side-effect audit and author-editable formal commit plan."""

        chapter = dict(outline_chain.get("chapter", {}).get("payload") or {})
        content = candidate.final_content
        normalized_content = normalize_evidence_text(content)
        required_events = [str(value) for value in chapter.get("required_events") or [] if str(value).strip()]
        forbidden_events = [str(value) for value in chapter.get("forbidden_events") or [] if str(value).strip()]
        required = [
            {
                "event": event,
                "status": "completed"
                if normalize_evidence_text(event) and normalize_evidence_text(event) in normalized_content
                else "unverified",
                "evidence": event
                if normalize_evidence_text(event) and normalize_evidence_text(event) in normalized_content
                else "",
            }
            for event in required_events
        ]
        raw_semantic_review = (
            generation_result.get("semantic_review")
            if isinstance(generation_result, dict)
            else None
        )
        # The real DAG gateway already paid for and persisted this review.  A
        # second call would only slow the chapter down and can disagree with
        # the trace the author sees.  Direct/legacy paths still need the same
        # full semantic review, even when required_events is empty or literal
        # evidence happens to be present.
        semantic_review: dict[str, Any] = (
            dict(raw_semantic_review)
            if isinstance(raw_semantic_review, dict)
            else await self._review_candidate_semantics(candidate, chapter, required_events)
        )
        coverage = {
            str(item.get("event") or ""): item
            for item in semantic_review.get("event_coverage", [])
            if isinstance(item, dict)
        }
        for item in required:
            reviewed = coverage.get(item["event"])
            if not reviewed:
                continue
            evidence = str(reviewed.get("evidence") or "").strip()
            normalized_evidence = normalize_evidence_text(evidence)
            if (
                reviewed.get("status") == "completed"
                and normalized_evidence
                and normalized_evidence in normalized_content
            ):
                item.update(status="completed", evidence=evidence)
            else:
                item.update(status="unverified", evidence="")
        required_events_complete = all(
            item["status"] == "completed" for item in required
        )
        semantic_issues = semantic_review.get("issues", [])
        semantic_review_ok = (
            str(semantic_review.get("status") or "").lower() == "approved"
            and not bool(semantic_review.get("machine_review_failed"))
            and not bool(semantic_review.get("machine_review_incomplete"))
            and not any(
                isinstance(issue, dict)
                and str(issue.get("severity") or "").lower() == "critical"
                for issue in semantic_issues
            )
        )
        forbidden_hits = [
            event
            for event in forbidden_events
            if normalize_evidence_text(event)
            and normalize_evidence_text(event) in normalized_content
        ]
        hard_blocks = [
            {
                "type": "forbidden_event",
                "message": f"正文命中了章纲禁止项：{event}",
                "event": event,
            }
            for event in forbidden_hits
        ]
        plan_actual = {
            "creative_goal": {
                "planned": str(chapter.get("creative_goal") or ""),
                "actual": "候选正文已生成，待作者确认",
            },
            "entry_state": str(chapter.get("entry_state") or ""),
            "exit_state": str(chapter.get("exit_state") or ""),
            "required_events": required,
            "forbidden_events": [{"event": event, "hit": event in forbidden_hits} for event in forbidden_events],
            "state_changes": dict(chapter.get("state_changes") or {}),
            "foreshadowing": dict(chapter.get("foreshadowing") or {}),
            "handoff_conditions": list(chapter.get("handoff_conditions") or []),
        }
        audit = {
            "status": "blocked" if hard_blocks else "reviewable",
            "hard_blocks": hard_blocks,
            "required_events_complete": required_events_complete,
            "soft_warnings": [
                f"计划必发生事件尚未获得正文完成证据：{item['event']}"
                for item in required
                if item["status"] != "completed"
            ],
            "plan_actual_comparison": plan_actual,
            "continuity": {
                "status": "verified" if required_events_complete else "requires_author_review"
            },
            "semantic_review": semantic_review,
            "style": {"status": "reviewed_by_dag" if isinstance(generation_result, dict) and generation_result.get("semantic_review") else "pending_author_or_model_review"},
            "ai_taste": {"status": "reviewed_by_dag" if isinstance(generation_result, dict) and generation_result.get("semantic_review") else "pending_author_or_model_review"},
            "dag": {
                "approved": bool(generation_result.get("approved")) and semantic_review_ok
                if isinstance(generation_result, dict)
                else False,
                "breaker_status": str(generation_result.get("breaker_status") or "") if isinstance(generation_result, dict) else "",
                "retry_exhausted": bool(generation_result.get("retry_exhausted")) if isinstance(generation_result, dict) else False,
                "review_required": bool(generation_result.get("review_required")) if isinstance(generation_result, dict) else True,
                "semantic_review": dict(generation_result.get("semantic_review") or {}) if isinstance(generation_result, dict) else {},
            },
        }
        script = generation_result.get("script", "") if isinstance(generation_result, dict) else ""
        commit_plan = {
            "chapter_summary": content[:500],
            "character_relation_location_world_deltas": {},
            "timeline_events": [
                item["event"] for item in required if item["status"] == "completed"
            ],
            "foreshadowing": {},
            "narrative_debt": {"introduced": [], "advanced": []},
            "next_chapter_handoff": [],
            "source": {
                "candidate_id": candidate.id,
                "generation_epoch": candidate.generation_epoch,
                "content_revision": candidate.content_revision,
                "chapter_outline_digest": str(outline_chain.get("chapter", {}).get("digest") or ""),
                "script": str(script),
            },
        }
        if isinstance(generation_result, dict) and generation_result.get("candidate_proposals"):
            commit_plan["dag_proposals"] = generation_result["candidate_proposals"]
        return audit, commit_plan

    async def _review_candidate_semantics(
        self,
        candidate: ChapterCandidate,
        chapter: dict[str, Any],
        required_events: list[str],
    ) -> dict[str, Any]:
        """Run the existing structured reviewer for every non-DAG candidate path."""

        if self.semantic_reviewer is None:
            return {
                "status": "unavailable",
                "machine_review_failed": True,
                "issues": [],
                "suggestions": [],
                "event_coverage": [],
            }
        try:
            from application.audit.services.chapter_ai_review_service import (
                serialize_chapter_ai_review_result,
            )
            from application.engine.dag.plan.schema import (
                chapter_rhythm_from_outline_payload,
                serialize_chapter_rhythm,
            )

            chapter_rhythm = serialize_chapter_rhythm(
                chapter_rhythm_from_outline_payload(chapter)
            )
            result = await self.semantic_reviewer.review(
                chapter_number=candidate.chapter_number,
                chapter_title=candidate.title,
                chapter_content=candidate.final_content,
                chapter_outline=json.dumps(
                    {
                        "creative_goal": chapter.get("creative_goal", ""),
                        "entry_state": chapter.get("entry_state", ""),
                        "exit_state": chapter.get("exit_state", ""),
                        "required_events": required_events,
                        "forbidden_events": chapter.get("forbidden_events", []),
                        "handoff_conditions": chapter.get("handoff_conditions", []),
                        "chapter_rhythm": chapter_rhythm,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                generation_hint=(
                    "审查人物身份、性格、动机与行为，知识边界，位置、身体和道具状态，"
                    "时间线、世界规则、已发生事件、伏笔、因果、AI Taste 和 Commercial Rhythm。"
                    "required/forbidden event 仍需给出正文证据；证据不足一律阻断或标记未完成。"
                ),
                required_events=required_events,
            )
            from application.engine.dag.plan.schema import chapter_rhythm_from_outline_payload

            review = serialize_chapter_ai_review_result(result)
            rhythm = review.get("rhythm_assessment") or {}
            review["machine_review_incomplete"] = bool(
                chapter_rhythm_from_outline_payload(chapter)
                and rhythm.get("status") != "complete"
            )
            return review
        except Exception as exc:
            return {
                "status": "unavailable",
                "machine_review_failed": True,
                "error": str(exc),
                "issues": [],
                "suggestions": [],
                "event_coverage": [],
            }


class _CandidateDAGObserver:
    """Adapt the DAG engine's existing observer hooks to durable candidate events."""

    def __init__(self, repository: ChapterCandidateRepository, dag_run_id: str) -> None:
        self.repository = repository
        self.dag_run_id = dag_run_id

    def on_node_start(self, _novel_id: str, node_id: str, node_type: str) -> None:
        self.repository.record_dag_event(
            self.dag_run_id, {"type": "node_started", "node_id": node_id, "node_type": node_type}
        )

    def on_node_complete(self, _novel_id: str, node_id: str, result: Any) -> None:
        self.repository.record_dag_event(
            self.dag_run_id,
            {
                "type": "node_completed",
                "node_id": node_id,
                "outputs": dict(getattr(result, "outputs", {}) or {}),
                "duration_ms": int(getattr(result, "duration_ms", 0) or 0),
            },
        )

    def on_node_error(self, _novel_id: str, node_id: str, error: Exception) -> None:
        self.repository.record_dag_event(
            self.dag_run_id, {"type": "node_failed", "node_id": node_id, "error": str(error)}
        )

    def on_node_bypassed(self, _novel_id: str, node_id: str) -> None:
        self.repository.record_dag_event(
            self.dag_run_id, {"type": "node_skipped", "node_id": node_id}
        )

    def on_candidate_event(self, event: dict[str, Any]) -> None:
        """Persist candidate-only streamed prose without promoting it to Canonical state."""

        if str(event.get("type") or "") != "prose_delta":
            return
        text = event.get("text")
        if not isinstance(text, str) or not text:
            return
        self.repository.record_dag_event(self.dag_run_id, event)
