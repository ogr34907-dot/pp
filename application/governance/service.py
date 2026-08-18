from __future__ import annotations

import re
import uuid
from dataclasses import replace
from typing import Any

from application.governance.models import (
    CanonicalStoryline,
    ChapterNarrativeBudget,
    GovernanceIssue,
    GovernanceReport,
    NarrativeContract,
    utc_now_iso,
)
from application.governance.storyline_registry import (
    canonical_id_for,
    merge_aliases,
    normalize_storyline_key,
)
from application.world.services.narrative_promise import extract_narrative_promise


_EARLY_PAYOFF_TERMS = (
    "真相大白",
    "彻底平反",
    "彻底解决",
    "身份揭晓",
    "幕后黑手现身",
    "大仇得报",
    "飞升成功",
    "终局",
)


class NarrativeGovernanceService:
    """Book-level narrative coordinator.

    This first production slice is deliberately deterministic. It creates a
    stable contract, canonical storyline surface, chapter budget and report
    records without taking over generation internals in one risky move.
    """

    def __init__(
        self,
        repository: Any,
        novel_repository: Any | None = None,
        legacy_storyline_repository: Any | None = None,
        db: Any | None = None,
        hierarchy_gate: Any | None = None,
        story_node_repository: Any | None = None,
    ) -> None:
        self.repository = repository
        self.novel_repository = novel_repository
        self.legacy_storyline_repository = legacy_storyline_repository
        self.db = db
        self.hierarchy_gate = hierarchy_gate
        self.story_node_repository = story_node_repository
        self._hierarchy_reports: dict[tuple[str, str], dict[str, Any]] = {}
        self._hierarchy_report_objects: dict[tuple[str, str], Any] = {}

    def _hierarchy_gate(self) -> Any:
        if self.hierarchy_gate is None:
            from application.engine.services.hierarchical_narrative_alignment_gate import (
                HierarchicalNarrativeAlignmentGate,
            )

            self.hierarchy_gate = HierarchicalNarrativeAlignmentGate(
                story_node_repository=self.story_node_repository
            )
        return self.hierarchy_gate

    def _hierarchy_nodes(self, novel_id: str) -> list[Any]:
        repository = self.story_node_repository
        if repository is None and self.db is not None:
            try:
                from infrastructure.persistence.database.story_node_repository import StoryNodeRepository

                repository = self.story_node_repository = StoryNodeRepository(self.db)
            except Exception:
                repository = None
        if repository is None:
            return []
        for name in ("get_by_novel_sync", "list_by_novel", "get_by_novel"):
            operation = getattr(repository, name, None)
            if operation is None:
                continue
            try:
                result = operation(novel_id)
                if hasattr(result, "__await__"):
                    import asyncio

                    result = asyncio.run(result)
                return list(result or [])
            except Exception:
                continue
        return []

    @staticmethod
    def _alignment_payload(report: Any) -> dict[str, Any]:
        data = report.to_dict() if hasattr(report, "to_dict") else dict(report)
        violations = data.get("violations") or []
        refs: list[str] = []
        for violation in violations:
            refs.extend(str(ref) for ref in (violation.get("evidence_refs") or []))
        data["evidence_refs"] = list(dict.fromkeys(refs))
        # The UI/API use one stable vocabulary for the five gate states.
        if data.get("overridden"):
            data["decision"] = "overridden"
        elif data.get("evidence_degraded") and data.get("decision") == "pass":
            data["decision"] = "evidence_degraded"
        return data

    def preview_hierarchy_alignment(
        self,
        novel_id: str,
        chapter_id: str,
        candidate: dict[str, Any],
        *,
        nodes: list[Any] | None = None,
    ) -> dict[str, Any]:
        if not isinstance(candidate, dict):
            raise ValueError("candidate must be an object")
        gate = self._hierarchy_gate()
        resolved_nodes = self._hierarchy_nodes(novel_id) if nodes is None else nodes
        summary_visibility_repository = self.story_node_repository
        if (
            summary_visibility_repository is not None
            and getattr(gate, "story_node_repository", None) is None
        ):
            gate.story_node_repository = summary_visibility_repository
        snapshot = gate.build_snapshot(
            chapter_id,
            resolved_nodes,
            novel_id=novel_id,
            summary_visibility_repository=summary_visibility_repository,
        )
        report = gate.check(snapshot, candidate)
        payload = self._alignment_payload(report)
        payload["snapshot"] = snapshot.to_dict()
        self._hierarchy_reports[(novel_id, str(payload["candidate_digest"]))] = payload
        self._hierarchy_report_objects[(novel_id, str(payload["candidate_digest"]))] = report
        self._emit(novel_id, "HierarchyAlignmentEvaluated", snapshot.chapter_number, payload)
        return payload

    # Explicit status/preview aliases make the API seam discoverable.
    hierarchy_alignment_status = preview_hierarchy_alignment
    preview_hierarchy_alignment_status = preview_hierarchy_alignment
    get_hierarchy_alignment_status = preview_hierarchy_alignment

    def override_hierarchy_alignment(
        self,
        novel_id: str,
        candidate_digest: str | dict[str, Any],
        reason: str | None = None,
    ) -> dict[str, Any]:
        if isinstance(candidate_digest, dict):
            payload = candidate_digest
            candidate_digest = str(payload.get("candidate_digest") or "").strip()
            reason = payload.get("reason") if reason is None else reason
        digest = str(candidate_digest or "").strip()
        reason_text = str(reason or "").strip()
        if not digest or not reason_text:
            raise ValueError("candidate_digest and non-empty reason are required")
        list_events = getattr(self.repository, "list_events", None)
        if list_events is not None:
            try:
                for event in list_events(novel_id, "HierarchyAlignmentOverrideApplied", 100):
                    if str((event.get("payload") or {}).get("candidate_digest") or "") == digest:
                        raise ValueError("override has already been consumed")
            except ValueError:
                raise
            except Exception:
                pass
        report_data = self._hierarchy_reports.get((novel_id, digest))
        report = self._hierarchy_report_objects.get((novel_id, digest))
        if report_data is None or report is None:
            report_data, report = self._load_persisted_alignment_report(novel_id, digest)
        if report_data is None:
            raise ValueError("candidate digest is stale or unknown")
        if report_data.get("overridden"):
            raise ValueError("override has already been consumed")
        from application.engine.services.hierarchical_narrative_alignment_gate import OneShotOverride

        if report is None:
            raise ValueError("candidate digest is stale or unknown")
        overridden = self._hierarchy_gate().apply_override(report, OneShotOverride(digest, reason_text))
        payload = self._alignment_payload(overridden)
        payload["override_reason"] = reason_text
        self._hierarchy_reports[(novel_id, digest)] = payload
        self._emit(novel_id, "HierarchyAlignmentOverrideApplied", None, payload)
        return payload

    apply_hierarchy_override = override_hierarchy_alignment
    apply_hierarchy_one_shot_override = override_hierarchy_alignment

    def _load_persisted_alignment_report(self, novel_id: str, digest: str) -> tuple[dict[str, Any] | None, Any | None]:
        list_events = getattr(self.repository, "list_events", None)
        if list_events is None:
            return None, None
        try:
            events = list_events(novel_id, "HierarchyAlignmentEvaluated", 100)
        except Exception:
            return None, None
        for event in events:
            payload = event.get("payload") or {}
            if str(payload.get("candidate_digest") or "") != digest:
                continue
            try:
                from application.engine.services.hierarchical_narrative_alignment_gate import (
                    AlignmentReport,
                    AlignmentViolation,
                    CharacterAgency,
                )

                violations = tuple(AlignmentViolation(**item) for item in payload.get("violations", []) if isinstance(item, dict))
                agency = tuple(CharacterAgency(**item) for item in payload.get("character_agency", []) if isinstance(item, dict))
                report = AlignmentReport(
                    decision=str(payload.get("decision", "review")),
                    confidence=float(payload.get("confidence", 0) or 0),
                    candidate_digest=digest,
                    snapshot_digest=str(payload.get("snapshot_digest", "")),
                    served_commitments=tuple(payload.get("served_commitments", [])),
                    missing_commitments=tuple(payload.get("missing_commitments", [])),
                    violations=violations,
                    character_agency=agency,
                    repair_plan=tuple(payload.get("repair_plan", [])),
                    evidence_degraded=bool(payload.get("evidence_degraded")),
                    overridden=False,
                )
                self._hierarchy_reports[(novel_id, digest)] = payload
                self._hierarchy_report_objects[(novel_id, digest)] = report
                return payload, report
            except Exception:
                return None, None
        return None, None

    def preview_hierarchy_replan(
        self,
        novel_id: str,
        chapter_id: str,
        candidate: dict[str, Any],
    ) -> dict[str, Any]:
        alignment = self.preview_hierarchy_alignment(novel_id, chapter_id, candidate)
        result = {
            "novel_id": novel_id,
            "chapter_id": chapter_id,
            "would_write": False,
            "mutations": [],
            "alignment_report": alignment,
            "repair_plan": alignment.get("repair_plan", []),
        }
        self._emit(novel_id, "HierarchyReplanPreviewed", alignment.get("chapter_number"), result)
        return result

    safe_replan_preview = preview_hierarchy_replan
    preview_replan = preview_hierarchy_replan

    def get_state(self, novel_id: str) -> dict[str, Any]:
        contract = self.get_or_create_contract(novel_id)
        self.backfill_canonical_storylines(novel_id)
        latest_report = self.repository.latest_report(novel_id)
        latest_hierarchy = None
        list_events = getattr(self.repository, "list_events", None)
        if list_events is not None:
            try:
                events = list_events(novel_id, "HierarchyAlignmentEvaluated", 1)
                latest_hierarchy = (events[0].get("payload") if events else None)
            except Exception:
                latest_hierarchy = None
        return {
            "contract": contract.to_dict(),
            "canonical_storylines": [s.to_dict() for s in self.repository.list_storylines(novel_id)],
            "open_debts": self.repository.list_open_debts(novel_id),
            "latest_report": latest_report.to_dict() if latest_report else None,
            "chapter_budget_preview": self.preview_chapter_budget(novel_id).to_dict(),
            "hierarchy_alignment": latest_hierarchy,
        }

    def get_or_create_contract(self, novel_id: str) -> NarrativeContract:
        existing = self.repository.get_contract(novel_id)
        if existing:
            return existing

        novel = self._load_novel(novel_id)
        title = getattr(novel, "title", "") if novel is not None else ""
        premise = getattr(novel, "premise", "") if novel is not None else ""
        promise = extract_narrative_promise(title, premise)
        anchors = list(promise.promise_keywords)
        if promise.genre_signal:
            anchors.insert(0, promise.genre_signal)

        contract = NarrativeContract(
            novel_id=novel_id,
            title_promise=promise.title or title or "未命名叙事承诺",
            core_question=promise.core_conflict or promise.opening_hook or premise[:180],
            theme_anchors=anchors[:8],
            forbidden_early_payoffs=[
                "前12章彻底解决书名反差",
                "前12章让核心敌人无代价退场",
                "前12章一次性公开身份/冤案/传承全部真相",
            ],
            reveal_budget={
                "opening": "hint",
                "development": "partial",
                "convergence": "major",
                "finale": "payoff",
            },
        )
        self.repository.save_contract(contract)
        self._emit(novel_id, "NarrativeContractCreated", None, contract.to_dict())
        return contract

    def update_contract(self, novel_id: str, payload: dict[str, Any]) -> NarrativeContract:
        current = self.get_or_create_contract(novel_id)
        contract = replace(
            current,
            title_promise=str(payload.get("title_promise", current.title_promise) or ""),
            core_question=str(payload.get("core_question", current.core_question) or ""),
            theme_anchors=list(payload.get("theme_anchors", current.theme_anchors) or []),
            forbidden_early_payoffs=list(
                payload.get("forbidden_early_payoffs", current.forbidden_early_payoffs) or []
            ),
            reveal_budget=dict(payload.get("reveal_budget", current.reveal_budget) or {}),
            updated_at=utc_now_iso(),
        )
        self.repository.save_contract(contract)
        self._emit(novel_id, "NarrativeContractUpdated", None, contract.to_dict())
        return contract

    def prepare_chapter(self, novel_id: str, chapter_number: int | None = None) -> dict[str, Any]:
        budget = self.preview_chapter_budget(novel_id, chapter_number)
        self._emit(novel_id, "ChapterPreparedEvent", budget.chapter_number, budget.to_dict())
        return {
            "budget": budget.to_dict(),
            "context_request": {
                "novel_id": novel_id,
                "chapter_number": budget.chapter_number,
                "promise_tags": budget.must_serve_promise_tags,
                "debt_ids": budget.carry_over_debt_ids,
            },
        }

    def preview_chapter_budget(
        self,
        novel_id: str,
        chapter_number: int | None = None,
    ) -> ChapterNarrativeBudget:
        if chapter_number is None:
            chapter_number = self._next_chapter_number(novel_id)
        contract = self.get_or_create_contract(novel_id)
        latest_report = self.repository.latest_report(novel_id)
        debts = self.repository.list_open_debts(novel_id, limit=8)
        stage = self._stage_for(novel_id, chapter_number)

        if stage == "opening":
            max_new = 0
            max_close = 1
            reveal = "hint"
        elif stage == "development":
            max_new = 1
            max_close = 2
            reveal = "partial"
        else:
            max_new = 0
            max_close = 3
            reveal = "major" if stage == "convergence" else "payoff"

        notes = []
        if latest_report and latest_report.severity in ("medium", "high", "critical"):
            notes.append("上一章治理报告要求本章优先修复承诺漂移或叙事债务。")
            max_new = max(0, max_new - 1)
        if stage in ("opening", "convergence", "finale"):
            notes.append("默认克制新增故事线；优先让现有线发生因果变化、交汇或回收。")

        return ChapterNarrativeBudget(
            novel_id=novel_id,
            chapter_number=int(chapter_number),
            max_new_storylines=max_new,
            max_debt_closures=max_close,
            allowed_reveal_level=reveal,
            must_serve_promise_tags=contract.theme_anchors[:4],
            carry_over_debt_ids=[str(d.get("debt_id") or d.get("id") or "") for d in debts[:5]],
            notes=notes,
        )

    def commit_chapter(
        self,
        novel_id: str,
        chapter_number: int,
        content: str,
        metadata: dict[str, Any] | None = None,
    ) -> GovernanceReport:
        self._emit(
            novel_id,
            "ChapterCommittedEvent",
            chapter_number,
            {"content_length": len(content or ""), "metadata": metadata or {}},
        )
        return self.evaluate_after_chapter(
            novel_id,
            chapter_number,
            content,
            sync_flags=metadata,
        )

    def evaluate_after_chapter(
        self,
        novel_id: str,
        chapter_number: int,
        content: str,
        sync_flags: dict[str, Any] | None = None,
    ) -> GovernanceReport:
        contract = self.get_or_create_contract(novel_id)
        issues: list[GovernanceIssue] = []
        hit_rate = self._promise_hit_rate(contract, content)

        if hit_rate <= 0:
            issues.append(
                GovernanceIssue(
                    code="promise_drift",
                    severity="high" if chapter_number <= 12 else "medium",
                    title="承诺漂移",
                    detail="本章正文没有命中书名承诺、核心问题或主题锚点。",
                    suggestion="下一章预算必须服务至少一个承诺标签，并减少新增支线。",
                )
            )
        elif hit_rate < 0.25:
            issues.append(
                GovernanceIssue(
                    code="promise_drift",
                    severity="medium",
                    title="承诺命中不足",
                    detail="本章只弱命中核心承诺，容易让读者感到主线退场。",
                    suggestion="补一处与书名反差或核心冲突直接相关的行动后果。",
                )
            )

        payoff_terms = [term for term in _EARLY_PAYOFF_TERMS if term in (content or "")]
        if chapter_number <= 12 and payoff_terms:
            issues.append(
                GovernanceIssue(
                    code="premature_payoff",
                    severity="high",
                    title="过早结清",
                    detail="开篇阶段出现疑似终局式兑现词。",
                    evidence=payoff_terms[:4],
                    suggestion="改为局部证据、误导性线索或带代价的小胜。",
                )
            )

        storylines = self.repository.list_storylines(novel_id)
        active_count = len([s for s in storylines if s.status == "active"])
        if chapter_number <= 20 and active_count > max(4, chapter_number // 3 + 2):
            issues.append(
                GovernanceIssue(
                    code="storyline_inflation",
                    severity="medium",
                    title="故事线膨胀",
                    detail=f"当前活跃 canonical storylines={active_count}，超过开篇承载能力。",
                    suggestion="合并同义线，下一章不要新增支线。",
                )
            )

        if sync_flags and sync_flags.get("causal_edges_stored") is False and chapter_number > 1:
            issues.append(
                GovernanceIssue(
                    code="causal_gap",
                    severity="low",
                    title="因果链偏弱",
                    detail="章后抽取未写入因果边，可能存在事件推进但缺少明确因果。",
                    suggestion="下一章强化上一章行动导致的具体后果。",
                )
            )

        severity = self._max_severity(issues)
        should_pause = severity in ("critical",) or (
            chapter_number <= 12
            and any(i.code == "promise_drift" and i.severity == "high" for i in issues)
            and any(i.code == "premature_payoff" for i in issues)
        )
        if should_pause and severity != "critical":
            severity = "critical"

        report = GovernanceReport(
            report_id=f"gov_{uuid.uuid4().hex}",
            novel_id=novel_id,
            chapter_number=chapter_number,
            severity=severity,
            promise_hit_rate=hit_rate,
            issues=issues,
            budget_patch=self._budget_patch_from_issues(issues),
            should_pause_autopilot=should_pause,
        )
        self.repository.save_report(report)
        self._emit(novel_id, "GovernanceEvaluatedEvent", chapter_number, report.to_dict())
        if should_pause:
            self._pause_autopilot(novel_id)
            self._emit(novel_id, "AutopilotPausedForGovernanceEvent", chapter_number, report.to_dict())
        return report

    def merge_storylines(self, novel_id: str, payload: dict[str, Any]) -> CanonicalStoryline:
        source_ids = [str(x) for x in payload.get("source_ids", []) if str(x).strip()]
        target_id = str(payload.get("target_id") or "").strip()
        title = str(payload.get("title") or "").strip()
        target = self.repository.get_storyline(target_id) if target_id else None
        sources = [self.repository.get_storyline(sid) for sid in source_ids]
        sources = [s for s in sources if s is not None]

        if not target:
            title = title or (sources[0].title if sources else "未命名故事线")
            target = CanonicalStoryline(
                canonical_id=canonical_id_for(novel_id, title),
                novel_id=novel_id,
                canonical_key=normalize_storyline_key(title),
                title=title,
            )

        target.aliases = merge_aliases(
            target.aliases,
            [s.title for s in sources],
            *(s.aliases for s in sources),
            payload.get("aliases", []),
        )
        target.source_storyline_ids = merge_aliases(
            target.source_storyline_ids,
            *(s.source_storyline_ids for s in sources),
            source_ids,
        )
        target.promise_tags = merge_aliases(target.promise_tags, payload.get("promise_tags", []))
        target.updated_at = utc_now_iso()
        self.repository.upsert_storyline(target)
        for source in sources:
            if source.canonical_id != target.canonical_id:
                self.repository.delete_storyline(source.canonical_id)
        self._emit(novel_id, "CanonicalStorylinesMerged", None, target.to_dict())
        return target

    def review_action(self, novel_id: str, payload: dict[str, Any]) -> dict[str, Any]:
        report_id = str(payload.get("report_id") or "").strip()
        action = str(payload.get("action") or "accepted").strip()
        status = {
            "accept": "accepted",
            "accepted": "accepted",
            "ignore": "ignored",
            "ignored": "ignored",
            "modify": "modified",
            "modified": "modified",
        }.get(action, action or "accepted")
        if report_id:
            self.repository.update_report_status(novel_id, report_id, status)
        self._emit(novel_id, "GovernanceReviewAction", None, {"report_id": report_id, "status": status})
        return {"report_id": report_id, "status": status}

    def backfill_canonical_storylines(self, novel_id: str) -> None:
        if not self.legacy_storyline_repository:
            return
        try:
            from domain.novel.value_objects.novel_id import NovelId

            legacy = self.legacy_storyline_repository.get_by_novel_id(NovelId(novel_id))
        except Exception:
            return
        existing_keys = {s.canonical_key for s in self.repository.list_storylines(novel_id)}
        for item in legacy:
            title = getattr(item, "name", "") or getattr(item, "description", "")[:40] or getattr(item, "id", "")
            key = normalize_storyline_key(title)
            if key in existing_keys:
                continue
            canonical = CanonicalStoryline(
                canonical_id=canonical_id_for(novel_id, title, [getattr(item, "id", "")]),
                novel_id=novel_id,
                canonical_key=key,
                title=title,
                aliases=merge_aliases([getattr(item, "description", "")]),
                goal=getattr(item, "progress_summary", "") or "",
                span={
                    "start": getattr(item, "estimated_chapter_start", None),
                    "end": getattr(item, "estimated_chapter_end", None),
                    "last_active": getattr(item, "last_active_chapter", None),
                },
                source_storyline_ids=[getattr(item, "id", "")],
            )
            self.repository.upsert_storyline(canonical)
            existing_keys.add(key)

    def _load_novel(self, novel_id: str) -> Any | None:
        if self.novel_repository:
            try:
                from domain.novel.value_objects.novel_id import NovelId

                return self.novel_repository.get_by_id(NovelId(novel_id))
            except Exception:
                return None
        if self.db:
            row = self.db.fetch_one("SELECT title, premise, target_chapters FROM novels WHERE id = ?", (novel_id,))
            return type("NovelRow", (), dict(row))() if row else None
        return None

    def _next_chapter_number(self, novel_id: str) -> int:
        if self.db:
            try:
                row = self.db.fetch_one("SELECT MAX(number) AS n FROM chapters WHERE novel_id = ?", (novel_id,))
                return int((row or {}).get("n") or 0) + 1
            except Exception:
                return 1
        return 1

    def _stage_for(self, novel_id: str, chapter_number: int) -> str:
        target = 0
        novel = self._load_novel(novel_id)
        if novel is not None:
            target = int(getattr(novel, "target_chapters", 0) or 0)
        if target <= 0:
            target = 80
        progress = max(0.0, min(1.0, chapter_number / target))
        if chapter_number <= 12 or progress <= 0.25:
            return "opening"
        if progress <= 0.75:
            return "development"
        if progress <= 0.9:
            return "convergence"
        return "finale"

    def _promise_hit_rate(self, contract: NarrativeContract, content: str) -> float:
        text = re.sub(r"\s+", "", content or "")
        if not text:
            return 0.0
        anchors = []
        anchors.extend(contract.theme_anchors)
        anchors.extend([contract.title_promise, contract.core_question])
        anchors = [a for a in anchors if a and len(str(a).strip()) >= 2]
        if not anchors:
            return 1.0
        hits = 0
        for anchor in anchors:
            terms = [anchor] if len(anchor) <= 12 else re.split(r"[，,。；;：:\s]+", anchor)
            if any(term and len(term) >= 2 and term in text for term in terms):
                hits += 1
        return round(hits / max(1, len(anchors)), 3)

    def _max_severity(self, issues: list[GovernanceIssue]) -> str:
        if not issues:
            return "info"
        order = {"info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}
        return max((issue.severity for issue in issues), key=lambda x: order.get(x, 0))

    def _budget_patch_from_issues(self, issues: list[GovernanceIssue]) -> dict[str, Any]:
        if not issues:
            return {}
        patch: dict[str, Any] = {"must_reduce_new_storylines": False, "must_retouch_promise": False}
        for issue in issues:
            if issue.code in ("promise_drift", "premature_payoff"):
                patch["must_retouch_promise"] = True
                patch["allowed_reveal_level"] = "hint"
            if issue.code == "storyline_inflation":
                patch["must_reduce_new_storylines"] = True
                patch["max_new_storylines_delta"] = -1
        return patch

    def _pause_autopilot(self, novel_id: str) -> None:
        if not self.db:
            return
        from infrastructure.persistence.database.chapter_candidate_repository import (
            CandidateGateError,
            ChapterCandidateRepository,
        )

        candidate_repository = ChapterCandidateRepository(self.db)
        try:
            run = candidate_repository.get_run(novel_id)
        except KeyError:
            # A missing Candidate run is the only condition that permits the
            # legacy novels mirror. Database or CAS failures must propagate.
            run = None

        if run is not None:
            if (
                run.current_candidate_id is not None
                and run.canonical_sync_status == "syncing"
                and run.next_action == "sync_candidate"
            ):
                candidate_repository.request_governance_pause_after_sync(
                    novel_id,
                    "narrative_governance_block",
                )
                return
            if run.current_candidate_id is None:
                candidate_repository.pause_for_governance(
                    novel_id,
                    "narrative_governance_block",
                )
                return
            raise CandidateGateError(
                "governance pause cannot safely transition the active Candidate run"
            )

        conn = self.db.get_connection()
        conn.execute(
            """
            UPDATE novels
            SET autopilot_status = 'stopped',
                current_stage = 'paused_for_review',
                audit_progress = ?
            WHERE id = ?
            """,
            ("叙事治理发现严重结构风险，已暂停自动驾驶。", novel_id),
        )
        conn.commit()

    def _emit(
        self,
        novel_id: str,
        event_type: str,
        chapter_number: int | None,
        payload: dict[str, Any],
    ) -> None:
        try:
            self.repository.append_event(
                f"gevt_{uuid.uuid4().hex}",
                novel_id,
                event_type,
                chapter_number,
                payload,
            )
        except Exception:
            return
