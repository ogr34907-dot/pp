"""Manifest-native generation of a complete direct-child outline cohort."""

from __future__ import annotations

import json
from typing import Any, Protocol, Sequence

from application.blueprint.services.outline_contract_service import OutlineContractService
from domain.ai.services.llm_service import GenerationConfig
from domain.ai.value_objects.prompt import Prompt
from domain.structure.outline_contract import OutlineLevel, OutlinePayload
from domain.structure.outline_plan import (
    PlanReconciliationReport,
    PlanReconciliationStatus,
    PlanRevisionStatus,
    PlanningAuthorityMode,
)
from domain.structure.outline_plan_validation import merge_author_locked_payload
from infrastructure.persistence.database.chapter_candidate_repository import (
    ChapterCandidateRepository,
    CandidateGateError,
)
from infrastructure.persistence.database.outline_contract_repository import (
    OutlineContractRepository,
    OutlineGateError,
)
from infrastructure.persistence.database.plan_projection_writer import PlanProjectionWriter


class OutlineCohortGenerationError(ValueError):
    """A provider response cannot safely become a complete cohort draft."""


class OutlineCohortLLM(Protocol):
    async def generate(self, prompt: Prompt, config: GenerationConfig) -> Any: ...


class OutlineCohortGenerationService:
    """Generate one manifest sibling cohort without changing the active plan."""

    def __init__(
        self,
        repository: OutlineContractRepository,
        contract_service: OutlineContractService,
        llm_service: OutlineCohortLLM,
        db: Any,
    ) -> None:
        self.repository = repository
        self.contract_service = contract_service
        self.llm_service = llm_service
        self.db = db

    def open_or_clone_cohort_draft(self, novel_id: str) -> Any:
        """Reuse a recoverable draft or anchor a new one to current Formal history."""

        formal_repository = ChapterCandidateRepository(
            self.repository._db or self.repository.db_path
        )

        def resolve_boundary(connection: Any) -> tuple[str, dict[str, int]]:
            try:
                formal_head, blockers = formal_repository.formal_history_snapshot(
                    novel_id, connection=connection
                )
            except CandidateGateError as exc:
                raise OutlineCohortGenerationError(
                    "cohort draft requires a proven current Formal boundary"
                ) from exc
            if blockers:
                raise OutlineCohortGenerationError(
                    "cohort draft requires a proven current Formal boundary: "
                    + ", ".join(blockers)
                )
            prefix = self.contract_service.compute_canonical_prefix(
                novel_id, formal_head, connection=connection
            )
            if not prefix.ready:
                raise OutlineCohortGenerationError(
                    "cohort draft requires a ready current Canonical boundary: "
                    + ", ".join(prefix.blockers)
                )
            return prefix.digest, {"formal_head": prefix.formal_head}

        try:
            return self.repository.clone_active_plan_draft(
                novel_id,
                canonical_boundary_resolver=resolve_boundary,
                recover_stale_pristine_draft=True,
            )
        except OutlineGateError as exc:
            if "requires explicit recovery" not in str(exc):
                raise
            raise OutlineCohortGenerationError(str(exc)) from exc

    async def generate_cohort(
        self,
        *,
        plan_revision_id: str,
        parent_logical_node_id: str,
        level: OutlineLevel,
        author_payloads: Sequence[OutlinePayload] = (),
    ) -> dict[str, Any]:
        plan = self.repository.get_plan_revision(plan_revision_id)
        parent = next(
            (item for item in plan.items if item.logical_node_id == parent_logical_node_id),
            None,
        )
        if parent is None or parent.level.child_level != level:
            raise OutlineCohortGenerationError("parent does not own the requested cohort level")
        prompt, scope, context_digest = self._build_prompt(plan, parent, level)
        attempt = self.repository.start_manifest_cohort_attempt(
            plan_revision_id=plan.id,
            parent_logical_node_id=parent.logical_node_id,
            level=level,
            scope=scope,
            context_digest=context_digest,
            prompt_snapshot={"system": prompt.system, "user": prompt.user},
        )
        try:
            response = await self.llm_service.generate(
                prompt, GenerationConfig(temperature=0.7)
            )
            raw = str(getattr(response, "content", response) or "")
            self.repository.append_manifest_cohort_attempt_delta(attempt["id"], raw)
            payloads = self._parse_payloads(raw)
            payloads, author_conflicts = self._merge_author_payloads(
                payloads, author_payloads
            )
            completed, updated = self.repository.complete_manifest_cohort_attempt_with_payloads(
                attempt_id=attempt["id"],
                payloads=payloads,
                expected_plan_digest=plan.digest,
                expected_parent_digest=parent.version_digest,
                expected_context_digest=context_digest,
                context_digest_supplier=lambda connection: self._current_context_digest(
                    plan_revision_id=plan.id,
                    parent_logical_node_id=parent.logical_node_id,
                    level=level,
                    connection=connection,
                ),
            )
            return {
                "attempt": completed,
                "plan": updated,
                "author_conflicts": author_conflicts,
            }
        except BaseException as exc:
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            self.repository.fail_manifest_cohort_attempt(attempt["id"], str(exc))
            raise

    async def publish_completed_cohort(self, *, attempt_id: str) -> dict[str, Any]:
        """Publish one completed draft cohort without invoking the LLM again."""

        conn = self.db.get_connection()
        if conn.in_transaction:
            raise OutlineCohortGenerationError(
                "cohort publication requires a clean database connection"
            )
        try:
            conn.execute("BEGIN IMMEDIATE")
            attempt = self.repository.get_manifest_cohort_attempt(
                attempt_id, _connection=conn
            )
            if attempt["status"] != "completed":
                raise OutlineCohortGenerationError(
                    "only a completed cohort attempt can be published"
                )
            plan = self.repository.get_plan_revision(
                str(attempt["plan_revision_id"]), _connection=conn
            )
            head = conn.execute(
                """
                SELECT authority_mode, authority_generation, projection_generation,
                       active_plan_revision_id, active_plan_digest,
                       working_plan_revision_id
                FROM outline_planning_heads WHERE novel_id = ?
                """,
                (plan.novel_id,),
            ).fetchone()
            receipt = (
                self._stored_publication_receipt(plan, attempt_id)
                if plan.sealed_at
                else None
            )
            if receipt is not None and self._is_completed_publication_replay(
                attempt_id=attempt_id, plan=plan, head=head, receipt=receipt
            ):
                run_row = conn.execute(
                    "SELECT * FROM novel_generation_runs WHERE novel_id = ?",
                    (plan.novel_id,),
                ).fetchone()
                if (
                    run_row is None
                    or int(run_row["generation_epoch"] or 0)
                    != receipt["generation_epoch"]
                ):
                    raise OutlineCohortGenerationError(
                        "cohort publication replay belongs to a retired generation"
                    )
                report = self._stored_reconciliation_report(plan)
                conn.commit()
                return {
                    "attempt": attempt,
                    "plan": plan,
                    "reconciliation": report,
                    "run": ChapterCandidateRepository._run_from_row(run_row),
                }
            if (
                head is None
                or str(head["authority_mode"] or "")
                != PlanningAuthorityMode.MANIFEST.value
                or str(head["working_plan_revision_id"] or "") != plan.id
            ):
                raise OutlineCohortGenerationError(
                    "cohort draft is no longer the manifest working revision"
                )
            if plan.sealed_at or plan.status not in {
                PlanRevisionStatus.DRAFT,
                PlanRevisionStatus.GENERATING,
                PlanRevisionStatus.VALIDATING,
            }:
                raise OutlineCohortGenerationError(
                    "cohort draft is no longer editable"
                )
            waiting = conn.execute(
                "SELECT * FROM novel_generation_runs WHERE novel_id = ?",
                (plan.novel_id,),
            ).fetchone()
            if (
                waiting is None
                or str(waiting["state"] or "") != "waiting_planning"
                or str(waiting["next_action"] or "") != "expand_outline_cohort"
                or waiting["current_candidate_id"] is not None
                or waiting["current_candidate_chapter"] is not None
                or str(waiting["canonical_sync_status"] or "") != "ready"
            ):
                raise OutlineCohortGenerationError(
                    "cohort publication requires an exact waiting planning run"
                )
            parent = next(
                (
                    item
                    for item in plan.items
                    if item.logical_node_id == attempt["parent_logical_node_id"]
                ),
                None,
            )
            if (
                parent is None
                or parent.level.child_level is None
                or parent.level.child_level.value != attempt["level"]
                or not any(
                    item.parent_logical_node_id == parent.logical_node_id
                    for item in plan.items
                )
            ):
                raise OutlineCohortGenerationError(
                    "completed cohort no longer matches its draft scope"
                )

            report = self.contract_service.reconcile_plan_boundary(
                novel_id=plan.novel_id, plan_revision_id=plan.id
            )
            if report.status != PlanReconciliationStatus.ALIGNED:
                raise OutlineCohortGenerationError(
                    "cohort publication requires an aligned formal boundary"
                )
            report_payload = {
                "plan_revision_id": plan.id,
                "status": report.status.value,
                "expected_formal_head": report.expected_formal_head,
                "actual_formal_head": report.actual_formal_head,
                "expected_prefix_digest": report.expected_prefix_digest,
                "actual_prefix_digest": report.actual_prefix_digest,
                "canonical_ready": report.canonical_ready,
                "memory_ready": report.memory_ready,
                "blockers": list(report.blockers),
                "publication": {
                    "attempt_id": attempt_id,
                    "authority_generation": int(head["authority_generation"] or 0)
                    + 1,
                    "projection_generation": int(head["projection_generation"] or 0)
                    + 1,
                    "generation_epoch": int(waiting["generation_epoch"] or 0),
                },
            }
            recorded = conn.execute(
                """
                UPDATE outline_plan_revisions
                SET reconciliation_status = ?, reconciliation_report_json = ?,
                    publish_idempotency_key = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ? AND sealed_at IS NULL AND status = ? AND digest = ?
                  AND publish_idempotency_key = ''
                """,
                (
                    PlanReconciliationStatus.ALIGNED.value,
                    json.dumps(report_payload, ensure_ascii=False, sort_keys=True),
                    attempt_id,
                    plan.id,
                    plan.status.value,
                    plan.digest,
                ),
            )
            if recorded.rowcount != 1:
                raise OutlineCohortGenerationError(
                    "cohort draft changed before reconciliation was recorded"
                )
            sealed = self.repository._seal_plan_revision_locked(
                conn, plan.id, clear_working_plan=False
            )
            await PlanProjectionWriter(
                self.contract_service.story_node_repository
            ).apply_bound_projection(
                conn,
                novel_id=plan.novel_id,
                plan_revision_id=sealed.id,
                expected_active_plan_revision_id=head["active_plan_revision_id"],
                expected_active_plan_digest=str(head["active_plan_digest"] or ""),
                expected_authority_generation=int(head["authority_generation"] or 0),
                expected_projection_generation=int(head["projection_generation"] or 0),
                expected_working_plan_revision_id=sealed.id,
            )

            resumed_run = None
            resumed_run = ChapterCandidateRepository(self.db).resume_after_outline_publication(
                conn,
                novel_id=plan.novel_id,
                expected_generation_epoch=int(waiting["generation_epoch"] or 0),
                expected_current_formal_chapter=int(
                    waiting["current_formal_chapter"] or 0
                ),
                expected_active_plan_revision_id=sealed.id,
                expected_active_plan_digest=sealed.digest,
                expected_authority_generation=int(head["authority_generation"] or 0)
                + 1,
                expected_projection_generation=int(head["projection_generation"] or 0)
                + 1,
            )
            conn.commit()
            return {
                "attempt": attempt,
                "plan": sealed,
                "reconciliation": report,
                "run": resumed_run,
            }
        except BaseException:
            if conn.in_transaction:
                conn.rollback()
            raise

    @staticmethod
    def _stored_publication_receipt(
        plan: Any, attempt_id: str
    ) -> dict[str, int | str]:
        payload = dict(plan.reconciliation_report or {})
        try:
            receipt = dict(payload["publication"])
            if (
                not isinstance(receipt["attempt_id"], str)
                or receipt["attempt_id"] != str(attempt_id)
                or str(plan.publish_idempotency_key or "") != str(attempt_id)
            ):
                raise ValueError("stored publication attempt changed")
            authority_generation = receipt["authority_generation"]
            projection_generation = receipt["projection_generation"]
            generation_epoch = receipt["generation_epoch"]
            if (
                not isinstance(authority_generation, int)
                or isinstance(authority_generation, bool)
                or not isinstance(projection_generation, int)
                or isinstance(projection_generation, bool)
                or not isinstance(generation_epoch, int)
                or isinstance(generation_epoch, bool)
                or authority_generation < 1
                or projection_generation != authority_generation
                or generation_epoch < 0
            ):
                raise ValueError("stored publication receipt is invalid")
            return {
                "attempt_id": str(attempt_id),
                "authority_generation": authority_generation,
                "projection_generation": projection_generation,
                "generation_epoch": generation_epoch,
            }
        except (KeyError, TypeError, ValueError) as exc:
            raise OutlineCohortGenerationError(
                "cohort publication has no valid immutable receipt"
            ) from exc

    @staticmethod
    def _stored_reconciliation_report(plan: Any) -> PlanReconciliationReport:
        payload = dict(plan.reconciliation_report or {})
        try:
            if not isinstance(payload["status"], str):
                raise ValueError("stored reconciliation status is invalid")
            status = PlanReconciliationStatus(payload["status"])
            report_plan_id = payload.get("plan_revision_id", plan.id)
            if not isinstance(report_plan_id, str):
                raise ValueError("stored reconciliation plan identity is invalid")
            if report_plan_id != plan.id or status != plan.reconciliation_status:
                raise ValueError("stored reconciliation identity changed")
            expected_formal_head = payload["expected_formal_head"]
            actual_formal_head = payload["actual_formal_head"]
            expected_prefix_digest = payload["expected_prefix_digest"]
            actual_prefix_digest = payload["actual_prefix_digest"]
            canonical_ready = payload["canonical_ready"]
            memory_ready = payload["memory_ready"]
            blockers = payload.get("blockers", [])
            if (
                not isinstance(expected_formal_head, int)
                or isinstance(expected_formal_head, bool)
                or not isinstance(actual_formal_head, int)
                or isinstance(actual_formal_head, bool)
                or not isinstance(expected_prefix_digest, str)
                or not isinstance(actual_prefix_digest, str)
                or not isinstance(canonical_ready, bool)
                or not isinstance(memory_ready, bool)
                or not isinstance(blockers, list)
                or not all(isinstance(blocker, str) for blocker in blockers)
            ):
                raise ValueError("stored reconciliation payload is invalid")
            return PlanReconciliationReport(
                plan_revision_id=plan.id,
                status=status,
                expected_formal_head=expected_formal_head,
                actual_formal_head=actual_formal_head,
                expected_prefix_digest=expected_prefix_digest,
                actual_prefix_digest=actual_prefix_digest,
                canonical_ready=canonical_ready,
                memory_ready=memory_ready,
                blockers=tuple(blockers),
            )
        except (KeyError, TypeError, ValueError, json.JSONDecodeError) as exc:
            raise OutlineCohortGenerationError(
                "cohort publication replay has no valid stored reconciliation"
            ) from exc

    @classmethod
    def _is_completed_publication_replay(
        cls,
        *,
        attempt_id: str,
        plan: Any,
        head: Any,
        receipt: dict[str, int | str],
    ) -> bool:
        return bool(
            head is not None
            and str(head["authority_mode"] or "")
            == PlanningAuthorityMode.MANIFEST.value
            and str(head["active_plan_revision_id"] or "") == plan.id
            and str(head["active_plan_digest"] or "") == plan.digest
            and head["working_plan_revision_id"] is None
            and int(head["authority_generation"] or 0)
            == int(receipt["authority_generation"])
            and int(head["projection_generation"] or 0)
            == int(receipt["projection_generation"])
            and plan.sealed_at
            and plan.status == PlanRevisionStatus.READY_FOR_REVIEW
            and plan.reconciliation_status == PlanReconciliationStatus.ALIGNED
            and str(plan.publish_idempotency_key or "") == str(attempt_id)
        )

    def _build_prompt(
        self,
        plan: Any,
        parent: Any,
        level: OutlineLevel,
        *,
        connection: Any = None,
    ) -> tuple[Prompt, dict[str, Any], str]:
        conn = connection if connection is not None else self.db.get_connection()
        novel = conn.execute(
            "SELECT title, premise, target_chapters FROM novels WHERE id = ?",
            (plan.novel_id,),
        ).fetchone()
        if novel is None:
            raise KeyError(f"novel not found: {plan.novel_id}")
        rows = self.repository.active_plan_items_with_payload(
            plan.novel_id, _connection=conn
        )
        by_logical = {str(row["logical_node_id"]): row for row in rows}
        parent_row = by_logical.get(parent.logical_node_id)
        if parent_row is None:
            raise OutlineCohortGenerationError("active manifest parent is unavailable")
        bible = self._bible_context(plan.novel_id, connection=conn)
        boundary = dict(plan.canonical_boundary or {})
        sibling_rows = [
            row
            for row in rows
            if row.get("parent_logical_node_id") == parent.logical_node_id
        ]
        scope = {
            "plan_digest": plan.digest,
            "parent_logical_node_id": parent.logical_node_id,
            "parent_version_digest": parent.version_digest,
            "level": level.value,
            "target_chapters": int(novel["target_chapters"] or 0),
        }
        context = {
            "author_intent": plan.author_intent,
            "canonical_boundary": boundary,
            "story_bible": bible,
            "current_parent": parent_row,
            "current_sibling_overview": sibling_rows,
            "target_chapters": scope["target_chapters"],
            "target_ending": self._payload_value(rows, OutlineLevel.OUTLINE, "exit_state"),
        }
        encoded_context = json.dumps(context, ensure_ascii=False, sort_keys=True)
        import hashlib

        context_digest = hashlib.sha256(encoded_context.encode("utf-8")).hexdigest()
        label = {
            OutlineLevel.PART: "全部部纲",
            OutlineLevel.VOLUME: "该部全部卷纲",
            OutlineLevel.ACT: "该卷全部幕纲",
            OutlineLevel.CHAPTER: "该幕全部章纲",
        }[level]
        prompt = Prompt(
            system=(
                "你是长篇小说规划助手。只输出 JSON 数组，数组元素是完整同级规划。"
                "不得修改 Canonical 已发生事实，不得输出 Markdown。"
            ),
            user=(
                f"小说：{novel['title']}\n创意：{novel['premise'] or ''}\n"
                f"现在整体生成：{label}\n"
                f"规划上下文：{encoded_context}\n"
                "每项必须包含 title、narrative_text、creative_goal、entry_state、"
                "exit_state、conflicts、state_changes、handoff_conditions、"
                "chapter_start、chapter_end。相邻项必须首尾连续，最后一项到达父级 exit_state。"
            ),
        )
        context_digest = hashlib.sha256(
            json.dumps(
                {"system": prompt.system, "user": prompt.user},
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
        ).hexdigest()
        return prompt, scope, context_digest

    def _bible_context(
        self, novel_id: str, *, connection: Any = None
    ) -> dict[str, list[dict[str, str]]]:
        conn = connection if connection is not None else self.db.get_connection()
        tables = (
            ("world_settings", "bible_world_settings"),
            ("characters", "unified_characters"),
            ("locations", "bible_locations"),
        )
        result: dict[str, list[dict[str, str]]] = {}
        for key, table in tables:
            rows = conn.execute(
                f"SELECT name, description FROM {table} WHERE novel_id = ? ORDER BY id LIMIT 20",
                (novel_id,),
            ).fetchall()
            if rows:
                result[key] = [
                    {"name": str(row["name"] or ""), "description": str(row["description"] or "")}
                    for row in rows
                ]
        return result

    def _current_context_digest(
        self,
        *,
        plan_revision_id: str,
        parent_logical_node_id: str,
        level: OutlineLevel,
        connection: Any,
    ) -> str:
        plan = self.repository.get_plan_revision(
            plan_revision_id, _connection=connection
        )
        parent = next(
            (
                item
                for item in plan.items
                if item.logical_node_id == parent_logical_node_id
            ),
            None,
        )
        if parent is None or parent.level.child_level != level:
            raise OutlineCohortGenerationError(
                "cohort context no longer matches its draft scope"
            )
        return self._build_prompt(plan, parent, level, connection=connection)[2]

    @staticmethod
    def _payload_value(
        rows: Sequence[dict[str, Any]], level: OutlineLevel, field: str
    ) -> str:
        row = next((item for item in rows if item.get("level") == level.value), None)
        payload = dict(row.get("payload") or {}) if row else {}
        return str(payload.get(field) or "")

    @staticmethod
    def _parse_payloads(raw: str) -> tuple[OutlinePayload, ...]:
        text = str(raw or "").strip()
        if text.startswith("```") and text.endswith("```"):
            text = text.split("\n", 1)[-1].rsplit("```", 1)[0].strip()
        try:
            values = json.loads(text)
        except json.JSONDecodeError as exc:
            raise OutlineCohortGenerationError("cohort generation requires a JSON array") from exc
        if not isinstance(values, list) or not values or not all(isinstance(item, dict) for item in values):
            raise OutlineCohortGenerationError("cohort generation requires a non-empty JSON object array")
        return tuple(OutlinePayload.from_dict(item) for item in values)

    @staticmethod
    def _merge_author_payloads(
        inferred: Sequence[OutlinePayload],
        authored: Sequence[OutlinePayload],
    ) -> tuple[tuple[OutlinePayload, ...], tuple[tuple[int, tuple[str, ...]], ...]]:
        if not authored:
            return tuple(inferred), ()
        if len(authored) != len(inferred):
            raise OutlineCohortGenerationError(
                "author cohort payload count must match the generated sibling cohort"
            )
        merged: list[OutlinePayload] = []
        conflicts: list[tuple[int, tuple[str, ...]]] = []
        for index, (author_payload, inferred_payload) in enumerate(zip(authored, inferred)):
            payload, fields = merge_author_locked_payload(author_payload, inferred_payload)
            merged.append(payload)
            if fields:
                conflicts.append((index, fields))
        return tuple(merged), tuple(conflicts)
