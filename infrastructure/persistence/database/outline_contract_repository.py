"""SQLite storage for draft/published five-level outline contracts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
import sqlite3
from typing import Any, Mapping, Optional, Sequence, Union
from uuid import uuid4

from domain.structure.outline_contract import (
    OutlineContract,
    OutlineLevel,
    OutlinePayload,
    OutlineSource,
    OutlineStatus,
)
from domain.structure.outline_plan import (
    BackfillResult,
    BackfillStatus,
    OutlinePlanItem,
    OutlinePlanRevision,
    PlanReconciliationStatus,
    PlanRevisionStatus,
    PlanningAuthorityMode,
    PlanningHead,
    canonical_plan_digest,
)
from domain.structure.outline_plan_validation import (
    ReplanImpactClosure,
    compute_replan_impact_closure,
    validate_sibling_cohort,
)
from infrastructure.persistence.database.planning_authority_guard import (
    assert_legacy_planning_mutation_allowed,
)


class OutlineGateError(ValueError):
    """Raised when an operation would bypass the published plan gate."""


@dataclass(frozen=True)
class OutlineContractSlot:
    """A logical outline node with independent draft and active revisions."""

    id: str
    novel_id: str
    level: OutlineLevel
    parent_contract_id: Optional[str]
    story_node_id: Optional[str]
    active: Optional[OutlineContract]
    draft: Optional[OutlineContract]
    author_locked: bool = False
    has_author_edits: bool = False

    @property
    def active_status(self) -> Optional[OutlineStatus]:
        return self.active.status if self.active else None


class OutlineContractRepository:
    """Persist immutable plan revisions and expose the active projection only."""

    def __init__(self, db: Union[str, "DatabaseConnection"]):
        from infrastructure.persistence.database.connection import DatabaseConnection

        if isinstance(db, DatabaseConnection):
            self._db = db
            self.db_path = db.db_path
        else:
            self._db = None
            self.db_path = str(db)

    def _connection(self) -> sqlite3.Connection:
        if self._db is not None:
            return self._db.get_connection()
        from infrastructure.persistence.database.connection import get_database

        return get_database(self.db_path).get_connection()

    def _assert_legacy_mutation(self, novel_id: str, operation: str) -> None:
        assert_legacy_planning_mutation_allowed(
            self._connection(), novel_id, operation=operation
        )

    def _begin_legacy_mutation_transaction(
        self,
        conn: sqlite3.Connection,
        novel_id: str,
        operation: str,
    ) -> None:
        """Fast-fail, then bind the authoritative legacy check to the writer lock."""

        if conn.in_transaction:
            raise OutlineGateError("legacy outline mutation requires a clean connection")
        self._assert_legacy_mutation(novel_id, operation)
        conn.execute("BEGIN IMMEDIATE")
        try:
            self._assert_legacy_mutation(novel_id, operation)
        except BaseException:
            conn.rollback()
            raise

    @staticmethod
    def _now() -> str:
        return datetime.now().isoformat()

    @staticmethod
    def _head_from_row(row: sqlite3.Row) -> PlanningHead:
        values = dict(row)
        return PlanningHead(
            novel_id=str(values["novel_id"]),
            authority_mode=PlanningAuthorityMode(str(values["authority_mode"])),
            authority_generation=int(values["authority_generation"]),
            active_plan_revision_id=values.get("active_plan_revision_id"),
            active_plan_digest=str(values.get("active_plan_digest") or ""),
            working_plan_revision_id=values.get("working_plan_revision_id"),
            projection_generation=int(values.get("projection_generation") or 0),
            auto_publish_repairable=bool(values.get("auto_publish_repairable")),
        )

    def ensure_planning_head(self, novel_id: str) -> PlanningHead:
        conn = self._connection()
        conn.execute(
            """
            INSERT OR IGNORE INTO outline_planning_heads (novel_id)
            VALUES (?)
            """,
            (novel_id,),
        )
        conn.commit()
        return self.get_planning_head(novel_id)

    def get_planning_head(self, novel_id: str) -> PlanningHead:
        row = self._connection().execute(
            "SELECT * FROM outline_planning_heads WHERE novel_id = ?",
            (novel_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"outline planning head not found: {novel_id}")
        return self._head_from_row(row)

    def _plan_items_from_rows(
        self, rows: Sequence[sqlite3.Row]
    ) -> tuple[OutlinePlanItem, ...]:
        return tuple(
            OutlinePlanItem(
                id=str(row["id"]),
                logical_node_id=str(row["logical_node_id"]),
                version_id=str(row["version_id"]),
                version_digest=str(row["version_digest"]),
                parent_logical_node_id=row["parent_logical_node_id"],
                level=OutlineLevel(str(row["level"])),
                sibling_index=int(row["sibling_index"]),
                expansion_state=str(row["expansion_state"]),
                validated_parent_digest=str(row["validated_parent_digest"] or ""),
                validated_previous_sibling_digest=str(
                    row["validated_previous_sibling_digest"] or ""
                ),
                is_reused=bool(row["is_reused"]),
            )
            for row in rows
        )

    def get_plan_revision(
        self,
        plan_revision_id: str,
        *,
        _connection: Optional[sqlite3.Connection] = None,
    ) -> OutlinePlanRevision:
        conn = _connection or self._connection()
        row = conn.execute(
            "SELECT * FROM outline_plan_revisions WHERE id = ?",
            (plan_revision_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"outline plan revision not found: {plan_revision_id}")
        item_rows = conn.execute(
            """
            SELECT item.*, version.digest AS version_digest
            FROM outline_plan_revision_items AS item
            JOIN outline_contract_versions AS version ON version.id = item.version_id
            WHERE item.plan_revision_id = ?
            ORDER BY CASE item.level
                WHEN 'outline' THEN 0 WHEN 'part' THEN 1 WHEN 'volume' THEN 2
                WHEN 'act' THEN 3 ELSE 4 END,
                COALESCE(item.parent_logical_node_id, ''), item.sibling_index,
                item.logical_node_id
            """,
            (plan_revision_id,),
        ).fetchall()
        values = dict(row)
        return OutlinePlanRevision(
            id=str(values["id"]),
            novel_id=str(values["novel_id"]),
            revision=int(values["revision"]),
            parent_plan_revision_id=values.get("parent_plan_revision_id"),
            status=PlanRevisionStatus(str(values["status"])),
            digest=str(values["digest"] or ""),
            base_plan_digest=str(values["base_plan_digest"] or ""),
            replan_start_chapter=values.get("replan_start_chapter"),
            canonical_prefix_digest=str(values["canonical_prefix_digest"] or ""),
            canonical_boundary=json.loads(values["canonical_boundary_json"] or "{}"),
            reconciliation_status=PlanReconciliationStatus(
                str(values["reconciliation_status"])
            ),
            reconciliation_report=json.loads(
                values["reconciliation_report_json"] or "{}"
            ),
            author_intent=str(values["author_intent"] or ""),
            created_by=str(values["created_by"] or ""),
            publish_idempotency_key=str(
                values["publish_idempotency_key"] or ""
            ),
            created_at=str(values["created_at"] or ""),
            updated_at=str(values["updated_at"] or ""),
            sealed_at=values.get("sealed_at"),
            items=self._plan_items_from_rows(item_rows),
        )

    def _require_active_manifest_plan(
        self,
        novel_id: str,
        *,
        conn: Optional[sqlite3.Connection] = None,
    ) -> OutlinePlanRevision:
        """Load and verify the exact immutable snapshot selected by a Head."""

        conn = conn or self._connection()
        head_row = conn.execute(
            "SELECT * FROM outline_planning_heads WHERE novel_id = ?",
            (novel_id,),
        ).fetchone()
        if head_row is None:
            raise OutlineGateError("manifest planning Head is missing")
        head = self._head_from_row(head_row)
        if head.authority_mode != PlanningAuthorityMode.MANIFEST:
            raise OutlineGateError("novel is not using manifest planning authority")
        if not head.active_plan_revision_id or not head.active_plan_digest:
            raise OutlineGateError("manifest planning Head has no active revision")
        if head.authority_generation != head.projection_generation:
            raise OutlineGateError("manifest planning Head projection is out of sync")

        plan = self.get_plan_revision(
            head.active_plan_revision_id,
            _connection=conn,
        )
        if plan.novel_id != novel_id:
            raise OutlineGateError("manifest planning Head targets another novel")
        if not plan.sealed_at:
            raise OutlineGateError("manifest planning Head is not sealed")
        if plan.status != PlanRevisionStatus.READY_FOR_REVIEW:
            raise OutlineGateError(
                f"active manifest revision is {plan.status.value}"
            )
        if plan.reconciliation_status != PlanReconciliationStatus.ALIGNED:
            raise OutlineGateError("active manifest revision is not aligned")
        if plan.digest != head.active_plan_digest:
            raise OutlineGateError("manifest planning Head digest does not match its revision")

        normalized_items = self._validate_plan_items(
            novel_id,
            plan.items,
            conn=conn,
            require_sealed_versions=True,
        )
        recomputed_digest = canonical_plan_digest(
            canonical_prefix_digest=plan.canonical_prefix_digest,
            items=normalized_items,
        )
        if recomputed_digest != plan.digest:
            raise OutlineGateError("active manifest revision digest is invalid")
        return plan

    def get_active_plan(self, novel_id: str) -> Optional[OutlinePlanRevision]:
        conn = self._connection()
        row = conn.execute(
            "SELECT active_plan_revision_id FROM outline_planning_heads WHERE novel_id = ?",
            (novel_id,),
        ).fetchone()
        if row is None or not row["active_plan_revision_id"]:
            return None
        head = self.get_planning_head(novel_id)
        if head.authority_mode == PlanningAuthorityMode.MANIFEST:
            return self._require_active_manifest_plan(novel_id, conn=conn)
        return self.get_plan_revision(str(row["active_plan_revision_id"]), _connection=conn)

    def active_plan_items_with_payload(self, novel_id: str) -> list[dict[str, Any]]:
        """Return the immutable active manifest topology and sealed payloads."""

        conn = self._connection()
        plan = self._require_active_manifest_plan(novel_id, conn=conn)
        rows = conn.execute(
            """
            SELECT item.id AS item_id, item.logical_node_id,
                   item.parent_logical_node_id, item.level, item.sibling_index,
                   item.expansion_state, item.validated_parent_digest,
                   item.validated_previous_sibling_digest, item.is_reused,
                   contract.novel_id, contract.story_node_id,
                   contract.parent_contract_id, contract.author_locked,
                   version.id AS version_id, version.revision AS version_revision,
                   version.digest AS version_digest, version.payload_json,
                   version.source AS version_source,
                   version.status AS version_status
            FROM outline_plan_revision_items AS item
            JOIN outline_contracts AS contract
              ON contract.id = item.logical_node_id
             AND contract.novel_id = ?
            JOIN outline_contract_versions AS version
              ON version.id = item.version_id
            WHERE item.plan_revision_id = ?
            ORDER BY CASE item.level
                WHEN 'outline' THEN 0 WHEN 'part' THEN 1 WHEN 'volume' THEN 2
                WHEN 'act' THEN 3 ELSE 4 END,
                COALESCE(item.parent_logical_node_id, ''), item.sibling_index,
                item.logical_node_id
            """,
            (novel_id, plan.id),
        ).fetchall()
        if len(rows) != len(plan.items):
            raise OutlineGateError("active manifest item set is incomplete")
        return [dict(row) for row in rows]

    def _validate_plan_items(
        self,
        novel_id: str,
        items: Sequence[OutlinePlanItem],
        *,
        conn: Optional[sqlite3.Connection] = None,
        require_sealed_versions: bool = False,
    ) -> tuple[OutlinePlanItem, ...]:
        if not items:
            raise OutlineGateError("outline plan requires at least one item")
        conn = conn or self._connection()
        normalized: list[OutlinePlanItem] = []
        logical_ids: set[str] = set()
        positions: set[tuple[str, str, int]] = set()
        for item in items:
            row = conn.execute(
                """
                SELECT contract.novel_id, contract.level, version.contract_id,
                       version.digest, version.payload_json, version.sealed_at
                FROM outline_contracts AS contract
                JOIN outline_contract_versions AS version
                  ON version.contract_id = contract.id
                WHERE contract.id = ? AND version.id = ?
                """,
                (item.logical_node_id, item.version_id),
            ).fetchone()
            if row is None or str(row["novel_id"]) != novel_id:
                raise OutlineGateError("outline plan item does not belong to the novel")
            if str(row["level"]) != item.level.value:
                raise OutlineGateError("outline plan item level does not match its contract")
            if str(row["digest"]) != item.version_digest:
                raise OutlineGateError("outline plan item version digest mismatch")
            if require_sealed_versions and not row["sealed_at"]:
                raise OutlineGateError("outline plan item content version is not sealed")
            try:
                payload = OutlinePayload.from_dict(
                    json.loads(str(row["payload_json"] or "{}"))
                )
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise OutlineGateError("outline plan item payload is invalid") from exc
            if payload.digest != item.version_digest:
                raise OutlineGateError("outline plan item payload digest mismatch")
            position = (
                item.parent_logical_node_id or "",
                item.level.value,
                item.sibling_index,
            )
            if item.logical_node_id in logical_ids:
                raise OutlineGateError("duplicate logical node in outline plan")
            if position in positions:
                raise OutlineGateError("duplicate sibling position in outline plan")
            logical_ids.add(item.logical_node_id)
            positions.add(position)
            normalized.append(item)
        roots = [
            item
            for item in normalized
            if item.level == OutlineLevel.OUTLINE and item.parent_logical_node_id is None
        ]
        if len(roots) != 1 or any(
            item.level == OutlineLevel.OUTLINE and item.parent_logical_node_id is not None
            for item in normalized
        ):
            raise OutlineGateError("outline plan requires exactly one root outline item")

        by_logical_id = {item.logical_node_id: item for item in normalized}
        for item in normalized:
            if item.level == OutlineLevel.OUTLINE:
                if item.validated_parent_digest:
                    raise OutlineGateError("outline root cannot declare a parent digest")
                continue
            if not item.parent_logical_node_id:
                raise OutlineGateError("non-root outline plan item requires a parent")
            parent = by_logical_id.get(item.parent_logical_node_id)
            if parent is None:
                raise OutlineGateError("outline plan item parent is missing from the plan")
            if parent.level.child_level != item.level:
                raise OutlineGateError(
                    f"outline plan parent {parent.level.value} cannot own {item.level.value}"
                )
            if item.validated_parent_digest != parent.version_digest:
                raise OutlineGateError(
                    "outline plan item validated_parent_digest does not match its parent version"
                )

        sibling_groups: dict[tuple[str, OutlineLevel], list[OutlinePlanItem]] = {}
        for item in normalized:
            sibling_groups.setdefault(
                (item.parent_logical_node_id or "", item.level), []
            ).append(item)
        for siblings in sibling_groups.values():
            siblings.sort(key=lambda item: item.sibling_index)
            for index, item in enumerate(siblings):
                if item.sibling_index != index:
                    raise OutlineGateError("outline plan sibling indexes must be contiguous")
                expected_previous = "" if index == 0 else siblings[index - 1].version_digest
                if item.validated_previous_sibling_digest != expected_previous:
                    raise OutlineGateError(
                        "outline plan sibling handoff digest does not match the previous version"
                    )
        return tuple(normalized)

    @staticmethod
    def _validate_plan_cohorts(
        conn: sqlite3.Connection,
        items: Sequence[OutlinePlanItem],
    ) -> None:
        """Validate expanded sibling payloads only when a plan is finalized."""

        by_logical_id = {item.logical_node_id: item for item in items}
        groups: dict[tuple[str, OutlineLevel], list[OutlinePlanItem]] = {}
        for item in items:
            if item.parent_logical_node_id:
                groups.setdefault((item.parent_logical_node_id, item.level), []).append(item)
        for siblings in groups.values():
            if len(siblings) < 2:
                continue
            siblings.sort(key=lambda item: item.sibling_index)
            parent = by_logical_id[siblings[0].parent_logical_node_id or ""]
            payloads = []
            for sibling in siblings:
                row = conn.execute(
                    "SELECT payload_json FROM outline_contract_versions WHERE id = ?",
                    (sibling.version_id,),
                ).fetchone()
                if row is None:
                    raise OutlineGateError("outline plan cohort payload is missing")
                payloads.append(
                    OutlinePayload.from_dict(json.loads(str(row["payload_json"] or "{}")))
                )
            parent_row = conn.execute(
                "SELECT payload_json FROM outline_contract_versions WHERE id = ?",
                (parent.version_id,),
            ).fetchone()
            if parent_row is None:
                raise OutlineGateError("outline plan cohort parent payload is missing")
            result = validate_sibling_cohort(
                level=siblings[0].level,
                parent_payload=OutlinePayload.from_dict(
                    json.loads(str(parent_row["payload_json"] or "{}"))
                ),
                siblings=payloads,
            )
            if result.blockers:
                raise OutlineGateError(
                    "outline plan cohort validation failed: " + ",".join(result.blockers)
                )

    @staticmethod
    def _insert_plan_items(
        conn: sqlite3.Connection,
        plan_revision_id: str,
        items: Sequence[OutlinePlanItem],
        now: str,
    ) -> None:
        for item in items:
            conn.execute(
                """
                INSERT INTO outline_plan_revision_items
                    (id, plan_revision_id, logical_node_id, version_id,
                     parent_logical_node_id, level, sibling_index,
                     expansion_state, validated_parent_digest,
                     validated_previous_sibling_digest, is_reused, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    item.id or f"outline-plan-item-{uuid4()}",
                    plan_revision_id,
                    item.logical_node_id,
                    item.version_id,
                    item.parent_logical_node_id,
                    item.level.value,
                    item.sibling_index,
                    item.expansion_state,
                    item.validated_parent_digest,
                    item.validated_previous_sibling_digest,
                    int(item.is_reused),
                    now,
                ),
            )

    def create_plan_draft(
        self,
        *,
        novel_id: str,
        items: Sequence[OutlinePlanItem],
        canonical_prefix_digest: str,
        canonical_boundary: Mapping[str, Any],
        parent_plan_revision_id: Optional[str] = None,
        base_plan_digest: str = "",
        replan_start_chapter: Optional[int] = None,
        reconciliation_status: PlanReconciliationStatus = (
            PlanReconciliationStatus.ALIGNED
        ),
        reconciliation_report: Optional[Mapping[str, Any]] = None,
        author_intent: str = "",
        created_by: str = "system",
        publish_idempotency_key: str = "",
    ) -> OutlinePlanRevision:
        head = self.ensure_planning_head(novel_id)
        if head.working_plan_revision_id:
            raise OutlineGateError("an outline plan draft is already open")
        normalized = self._validate_plan_items(novel_id, items)
        if parent_plan_revision_id:
            parent = self.get_plan_revision(parent_plan_revision_id)
            if parent.novel_id != novel_id or not parent.sealed_at:
                raise OutlineGateError("parent outline plan must be a sealed revision")
        digest = canonical_plan_digest(
            canonical_prefix_digest=canonical_prefix_digest,
            items=normalized,
        )
        conn = self._connection()
        revision = int(
            conn.execute(
                "SELECT COALESCE(MAX(revision), 0) + 1 AS revision "
                "FROM outline_plan_revisions WHERE novel_id = ?",
                (novel_id,),
            ).fetchone()["revision"]
        )
        plan_id = f"outline-plan-{uuid4()}"
        now = self._now()
        try:
            conn.execute("BEGIN")
            conn.execute(
                """
                INSERT INTO outline_plan_revisions
                    (id, novel_id, revision, parent_plan_revision_id, status,
                     digest, base_plan_digest, replan_start_chapter,
                     canonical_prefix_digest, canonical_boundary_json,
                     reconciliation_status, reconciliation_report_json,
                     author_intent, created_by, publish_idempotency_key,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, 'draft', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    plan_id,
                    novel_id,
                    revision,
                    parent_plan_revision_id,
                    digest,
                    base_plan_digest,
                    replan_start_chapter,
                    canonical_prefix_digest,
                    json.dumps(dict(canonical_boundary), ensure_ascii=False, sort_keys=True),
                    PlanReconciliationStatus(reconciliation_status).value,
                    json.dumps(
                        dict(reconciliation_report or {}),
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    author_intent,
                    created_by,
                    publish_idempotency_key,
                    now,
                    now,
                ),
            )
            self._insert_plan_items(conn, plan_id, normalized, now)
            conn.execute(
                """
                UPDATE outline_planning_heads
                SET working_plan_revision_id = ?, updated_at = ?
                WHERE novel_id = ? AND working_plan_revision_id IS NULL
                """,
                (plan_id, now, novel_id),
            )
            if conn.execute("SELECT changes()").fetchone()[0] != 1:
                raise OutlineGateError("an outline plan draft is already open")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return self.get_plan_revision(plan_id)

    def clone_active_plan_draft(
        self,
        novel_id: str,
        *,
        replan_start_chapter: Optional[int] = None,
        author_intent: str = "",
        created_by: str = "system",
    ) -> OutlinePlanRevision:
        """Clone the immutable active manifest into the book's only open draft.

        This is the shared starting point for future replanning and rolling
        cohort expansion. It deliberately does not mutate the active Head or
        physical StoryNode projection.
        """

        conn = self._connection()
        if conn.in_transaction:
            raise OutlineGateError("outline plan cloning requires a clean connection")
        now = self._now()
        try:
            conn.execute("BEGIN IMMEDIATE")
            head = self.get_planning_head(novel_id)
            if head.authority_mode != PlanningAuthorityMode.MANIFEST:
                raise OutlineGateError("active manifest planning authority is required")
            if head.working_plan_revision_id:
                raise OutlineGateError("an outline plan draft is already open")
            active = self._require_active_manifest_plan(novel_id, conn=conn)
            revision = int(
                conn.execute(
                    "SELECT COALESCE(MAX(revision), 0) + 1 AS revision "
                    "FROM outline_plan_revisions WHERE novel_id = ?",
                    (novel_id,),
                ).fetchone()["revision"]
            )
            plan_id = f"outline-plan-{uuid4()}"
            conn.execute(
                """
                INSERT INTO outline_plan_revisions
                    (id, novel_id, revision, parent_plan_revision_id, status,
                     digest, base_plan_digest, replan_start_chapter,
                     canonical_prefix_digest, canonical_boundary_json,
                     reconciliation_status, reconciliation_report_json,
                     author_intent, created_by, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'draft', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    plan_id,
                    novel_id,
                    revision,
                    active.id,
                    active.digest,
                    active.digest,
                    replan_start_chapter,
                    active.canonical_prefix_digest,
                    json.dumps(dict(active.canonical_boundary or {}), ensure_ascii=False, sort_keys=True),
                    active.reconciliation_status.value,
                    json.dumps(dict(active.reconciliation_report or {}), ensure_ascii=False, sort_keys=True),
                    author_intent,
                    created_by,
                    now,
                    now,
                ),
            )
            self._insert_plan_items(
                conn,
                plan_id,
                tuple(
                    OutlinePlanItem(
                        logical_node_id=item.logical_node_id,
                        version_id=item.version_id,
                        version_digest=item.version_digest,
                        level=item.level,
                        sibling_index=item.sibling_index,
                        parent_logical_node_id=item.parent_logical_node_id,
                        expansion_state=item.expansion_state,
                        validated_parent_digest=item.validated_parent_digest,
                        validated_previous_sibling_digest=(
                            item.validated_previous_sibling_digest
                        ),
                        is_reused=True,
                    )
                    for item in active.items
                ),
                now,
            )
            updated = conn.execute(
                """
                UPDATE outline_planning_heads
                SET working_plan_revision_id = ?, updated_at = ?
                WHERE novel_id = ? AND authority_mode = 'manifest'
                  AND active_plan_revision_id = ? AND active_plan_digest = ?
                  AND authority_generation = ? AND projection_generation = ?
                  AND working_plan_revision_id IS NULL
                """,
                (
                    plan_id,
                    now,
                    novel_id,
                    active.id,
                    active.digest,
                    head.authority_generation,
                    head.projection_generation,
                ),
            )
            if updated.rowcount != 1:
                raise OutlineGateError("active outline planning Head changed during cloning")
            conn.commit()
        except BaseException:
            if conn.in_transaction:
                conn.rollback()
            raise
        return self.get_plan_revision(plan_id)

    def replace_draft_cohort_payloads(
        self,
        *,
        plan_revision_id: str,
        parent_logical_node_id: str,
        payloads: Sequence[OutlinePayload],
        expected_parent_digest: Optional[str] = None,
        source: OutlineSource = OutlineSource.AI,
    ) -> OutlinePlanRevision:
        """Atomically create and replace one complete direct-child cohort."""

        if not payloads:
            raise OutlineGateError("manifest cohort must contain every direct child")
        conn = self._connection()
        if conn.in_transaction:
            raise OutlineGateError("manifest cohort replacement requires a clean connection")
        now = self._now()
        try:
            conn.execute("BEGIN IMMEDIATE")
            plan = self.get_plan_revision(plan_revision_id, _connection=conn)
            head = self.get_planning_head(plan.novel_id)
            if head.authority_mode != PlanningAuthorityMode.MANIFEST:
                raise OutlineGateError("manifest planning authority is required")
            if head.working_plan_revision_id != plan.id:
                raise OutlineGateError("manifest cohort must target the open plan draft")
            if plan.sealed_at or plan.status not in {
                PlanRevisionStatus.DRAFT,
                PlanRevisionStatus.GENERATING,
                PlanRevisionStatus.VALIDATING,
            }:
                raise OutlineGateError("manifest cohort requires an editable draft")
            parent = next(
                (item for item in plan.items if item.logical_node_id == parent_logical_node_id),
                None,
            )
            if parent is None:
                raise OutlineGateError("manifest cohort parent is not in the draft")
            expected_level = parent.level.child_level
            if expected_level is None:
                raise OutlineGateError("manifest cohort parent cannot own children")
            if expected_parent_digest and expected_parent_digest != parent.version_digest:
                raise OutlineGateError("manifest cohort parent digest changed")
            if any(
                item.parent_logical_node_id == parent_logical_node_id
                for item in plan.items
            ):
                raise OutlineGateError(
                    "manifest cohort replacement requires a new expansion; "
                    "future replanning must use the impact-closure workflow"
                )
            children: list[OutlinePlanItem] = []
            previous_digest = ""
            for sibling_index, payload in enumerate(payloads):
                contract_id = f"outline-{uuid4()}"
                version_id = f"outline-version-{uuid4()}"
                payload_json = json.dumps(
                    payload.canonical_dict(), ensure_ascii=False, sort_keys=True
                )
                conn.execute(
                    """
                    INSERT INTO outline_contracts
                        (id, novel_id, level, parent_contract_id, status, created_at, updated_at)
                    VALUES (?, ?, ?, ?, 'draft', ?, ?)
                    """,
                    (
                        contract_id,
                        plan.novel_id,
                        expected_level.value,
                        parent_logical_node_id,
                        now,
                        now,
                    ),
                )
                conn.execute(
                    """
                    INSERT INTO outline_contract_versions
                        (id, contract_id, revision, payload_json, digest,
                         parent_revision_digest, previous_sibling_digest, source,
                         status, created_at, updated_at)
                    VALUES (?, ?, 1, ?, ?, ?, ?, ?, 'draft', ?, ?)
                    """,
                    (
                        version_id,
                        contract_id,
                        payload_json,
                        payload.digest,
                        parent.version_digest,
                        previous_digest,
                        source.value,
                        now,
                        now,
                    ),
                )
                children.append(
                    OutlinePlanItem(
                        logical_node_id=contract_id,
                        version_id=version_id,
                        version_digest=payload.digest,
                        level=expected_level,
                        sibling_index=sibling_index,
                        parent_logical_node_id=parent_logical_node_id,
                        validated_parent_digest=parent.version_digest,
                        validated_previous_sibling_digest=previous_digest,
                    )
                )
                previous_digest = payload.digest
            retained = list(plan.items)
            normalized = self._validate_plan_items(
                plan.novel_id, (*retained, *children), conn=conn
            )
            digest = canonical_plan_digest(
                canonical_prefix_digest=plan.canonical_prefix_digest,
                items=normalized,
            )
            self._insert_plan_items(conn, plan.id, children, now)
            updated = conn.execute(
                """
                UPDATE outline_plan_revisions
                SET digest = ?, updated_at = ?
                WHERE id = ? AND sealed_at IS NULL AND status = ?
                """,
                (digest, now, plan.id, plan.status.value),
            )
            if updated.rowcount != 1:
                raise OutlineGateError("manifest draft changed during cohort replacement")
            conn.commit()
        except BaseException:
            if conn.in_transaction:
                conn.rollback()
            raise
        return self.get_plan_revision(plan_revision_id)

    def prepare_future_replan_draft(
        self,
        *,
        plan_revision_id: str,
        changed_logical_node_id: str,
    ) -> tuple[OutlinePlanRevision, ReplanImpactClosure]:
        """Remove a future impact closure from an open draft, never history."""

        conn = self._connection()
        if conn.in_transaction:
            raise OutlineGateError("future replan preparation requires a clean connection")
        now = self._now()
        try:
            conn.execute("BEGIN IMMEDIATE")
            plan = self.get_plan_revision(plan_revision_id, _connection=conn)
            head = self.get_planning_head(plan.novel_id)
            if head.authority_mode != PlanningAuthorityMode.MANIFEST:
                raise OutlineGateError("manifest planning authority is required")
            if head.working_plan_revision_id != plan.id:
                raise OutlineGateError("future replan must target the open plan draft")
            if plan.sealed_at or plan.status not in {
                PlanRevisionStatus.DRAFT,
                PlanRevisionStatus.GENERATING,
                PlanRevisionStatus.VALIDATING,
            }:
                raise OutlineGateError("future replan requires an editable draft")
            closure = compute_replan_impact_closure(
                plan.items, changed_logical_node_id
            )
            remaining = tuple(
                OutlinePlanItem(
                    logical_node_id=item.logical_node_id,
                    version_id=item.version_id,
                    version_digest=item.version_digest,
                    level=item.level,
                    sibling_index=item.sibling_index,
                    parent_logical_node_id=item.parent_logical_node_id,
                    expansion_state=(
                        "unexpanded"
                        if item.logical_node_id in closure.ancestor_logical_node_ids
                        else item.expansion_state
                    ),
                    validated_parent_digest=item.validated_parent_digest,
                    validated_previous_sibling_digest=(
                        item.validated_previous_sibling_digest
                    ),
                    is_reused=item.is_reused,
                    id=item.id,
                )
                for item in plan.items
                if item.logical_node_id not in closure.invalidated_logical_node_ids
            )
            if not remaining:
                raise OutlineGateError("future replan cannot remove the outline root")
            normalized = self._validate_plan_items(
                plan.novel_id, remaining, conn=conn
            )
            digest = canonical_plan_digest(
                canonical_prefix_digest=plan.canonical_prefix_digest,
                items=normalized,
            )
            placeholders = ", ".join("?" for _ in closure.invalidated_logical_node_ids)
            conn.execute(
                f"DELETE FROM outline_plan_revision_items WHERE plan_revision_id = ? "
                f"AND logical_node_id IN ({placeholders})",
                (plan.id, *closure.invalidated_logical_node_ids),
            )
            for item in normalized:
                if item.logical_node_id in closure.ancestor_logical_node_ids:
                    conn.execute(
                        "UPDATE outline_plan_revision_items SET expansion_state = ? "
                        "WHERE plan_revision_id = ? AND logical_node_id = ?",
                        (item.expansion_state, plan.id, item.logical_node_id),
                    )
            updated = conn.execute(
                """
                UPDATE outline_plan_revisions
                SET digest = ?, updated_at = ?
                WHERE id = ? AND sealed_at IS NULL AND status = ?
                """,
                (digest, now, plan.id, plan.status.value),
            )
            if updated.rowcount != 1:
                raise OutlineGateError("future replan draft changed during preparation")
            conn.commit()
        except BaseException:
            if conn.in_transaction:
                conn.rollback()
            raise
        return self.get_plan_revision(plan_revision_id), closure

    def seal_plan_revision(self, plan_revision_id: str) -> OutlinePlanRevision:
        conn = self._connection()
        if conn.in_transaction:
            raise OutlineGateError("outline plan sealing requires a clean connection")
        now = self._now()
        try:
            # Read every mutable input only after the write lock is held.  A
            # plan digest is meaningful only for this exact locked snapshot.
            conn.execute("BEGIN IMMEDIATE")
            plan = self.get_plan_revision(plan_revision_id, _connection=conn)
            if plan.sealed_at:
                conn.commit()
                return plan
            if plan.status not in {
                PlanRevisionStatus.DRAFT,
                PlanRevisionStatus.GENERATING,
                PlanRevisionStatus.VALIDATING,
            }:
                raise OutlineGateError("only an editable outline plan can be sealed")
            normalized_items = self._validate_plan_items(
                plan.novel_id, plan.items, conn=conn
            )
            self._validate_plan_cohorts(conn, normalized_items)
            digest = canonical_plan_digest(
                canonical_prefix_digest=plan.canonical_prefix_digest,
                items=normalized_items,
            )
            if digest != plan.digest:
                raise OutlineGateError("outline plan digest changed before sealing")
            existing = conn.execute(
                """
                SELECT id FROM outline_plan_revisions
                WHERE novel_id = ? AND digest = ? AND sealed_at IS NOT NULL
                  AND id <> ?
                """,
                (plan.novel_id, plan.digest, plan.id),
            ).fetchone()
            if existing is not None:
                conn.execute(
                    """
                    UPDATE outline_planning_heads
                    SET working_plan_revision_id = NULL, updated_at = ?
                    WHERE novel_id = ? AND working_plan_revision_id = ?
                    """,
                    (now, plan.novel_id, plan.id),
                )
                deleted = conn.execute(
                    "DELETE FROM outline_plan_revisions WHERE id = ? AND sealed_at IS NULL",
                    (plan.id,),
                )
                if deleted.rowcount != 1:
                    raise OutlineGateError("outline plan changed during duplicate sealing")
                conn.commit()
                return self.get_plan_revision(str(existing["id"]))
            conn.execute(
                """
                UPDATE outline_contract_versions
                SET sealed_at = ?
                WHERE sealed_at IS NULL
                  AND id IN (
                    SELECT version_id FROM outline_plan_revision_items
                    WHERE plan_revision_id = ?
                )
                """,
                (now, plan.id),
            )
            sealed = conn.execute(
                """
                UPDATE outline_plan_revisions
                SET status = 'ready_for_review', sealed_at = ?, updated_at = ?
                WHERE id = ? AND sealed_at IS NULL AND status = ? AND digest = ?
                """,
                (now, now, plan.id, plan.status.value, plan.digest),
            )
            if sealed.rowcount != 1:
                raise OutlineGateError("outline plan changed during sealing")
            cleared = conn.execute(
                """
                UPDATE outline_planning_heads
                SET working_plan_revision_id = NULL, updated_at = ?
                WHERE novel_id = ? AND working_plan_revision_id = ?
                """,
                (now, plan.novel_id, plan.id),
            )
            if cleared.rowcount != 1:
                raise OutlineGateError("outline planning Head changed during sealing")
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return self.get_plan_revision(plan.id)

    def _legacy_projection_items(self, novel_id: str) -> tuple[OutlinePlanItem, ...]:
        conn = self._connection()
        rows = conn.execute(
            """
            SELECT contract.id AS logical_node_id,
                   contract.level,
                   contract.parent_contract_id,
                   contract.story_node_id,
                   contract.status AS contract_status,
                   contract.active_version_id,
                   version.id AS version_id,
                   version.digest AS version_digest,
                   version.payload_json AS version_payload_json,
                   version.status AS version_status,
                   version.previous_sibling_digest,
                   projection.version_id AS projection_version_id,
                   projection.digest AS projection_digest,
                   projection.payload_json AS projection_payload_json,
                   node.order_index,
                   node.number
            FROM outline_contracts AS contract
            JOIN outline_contract_versions AS version
              ON version.id = contract.active_version_id
            JOIN outline_plan_projections AS projection
              ON projection.contract_id = contract.id
             AND projection.is_active = 1
            LEFT JOIN story_nodes AS node ON node.id = contract.story_node_id
            WHERE contract.novel_id = ?
            """,
            (novel_id,),
        ).fetchall()
        expected_count = int(
            conn.execute(
                """
                SELECT COUNT(*) AS count
                FROM outline_contracts
                WHERE novel_id = ? AND status = 'synced'
                  AND active_version_id IS NOT NULL
                """,
                (novel_id,),
            ).fetchone()["count"]
        )
        if not rows or len(rows) != expected_count:
            raise OutlineGateError("legacy projection set is incomplete")

        by_id = {str(row["logical_node_id"]): row for row in rows}
        if len(by_id) != len(rows):
            raise OutlineGateError("legacy projection contains duplicate active rows")
        roots = [
            row
            for row in rows
            if row["level"] == OutlineLevel.OUTLINE.value
            and row["parent_contract_id"] is None
        ]
        if len(roots) != 1:
            raise OutlineGateError("legacy projection requires exactly one outline root")

        children: dict[str, list[sqlite3.Row]] = {}
        for row in rows:
            logical_node_id = str(row["logical_node_id"])
            parent_id = row["parent_contract_id"]
            if str(row["contract_status"]) != OutlineStatus.SYNCED.value:
                raise OutlineGateError("legacy projection contains an unsynced contract")
            if str(row["active_version_id"]) != str(row["projection_version_id"]):
                raise OutlineGateError("legacy projection version does not match active version")
            version_digest = str(row["version_digest"] or "")
            if version_digest != str(row["projection_digest"] or ""):
                raise OutlineGateError("legacy projection digest does not match active version")
            try:
                version_payload = OutlinePayload.from_dict(
                    json.loads(row["version_payload_json"] or "{}")
                )
                projection_payload = OutlinePayload.from_dict(
                    json.loads(row["projection_payload_json"] or "{}")
                )
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise OutlineGateError("legacy projection payload is invalid") from exc
            if version_payload.digest != version_digest:
                raise OutlineGateError("legacy version payload digest is invalid")
            if projection_payload.digest != version_digest:
                raise OutlineGateError("legacy projection payload digest is invalid")
            if parent_id is not None:
                parent = by_id.get(str(parent_id))
                if parent is None:
                    raise OutlineGateError("legacy projection parent is missing")
                parent_level = OutlineLevel(str(parent["level"]))
                if parent_level.child_level != OutlineLevel(str(row["level"])):
                    raise OutlineGateError("legacy projection level hierarchy is invalid")
                children.setdefault(str(parent_id), []).append(row)
            elif logical_node_id != str(roots[0]["logical_node_id"]):
                raise OutlineGateError("legacy projection contains an orphan root")

        sibling_indexes: dict[str, int] = {str(roots[0]["logical_node_id"]): 0}
        previous_digests: dict[str, str] = {}
        for sibling_rows in children.values():
            ordered = sorted(
                sibling_rows,
                key=lambda row: (
                    int(row["order_index"]) if row["order_index"] is not None else 2**31,
                    int(row["number"]) if row["number"] is not None else 2**31,
                    str(row["logical_node_id"]),
                ),
            )
            previous_digest = ""
            for index, row in enumerate(ordered):
                logical_node_id = str(row["logical_node_id"])
                sibling_indexes[logical_node_id] = index
                previous_digests[logical_node_id] = previous_digest
                recorded = str(row["previous_sibling_digest"] or "")
                if recorded and recorded != previous_digest:
                    raise OutlineGateError("legacy sibling digest is inconsistent")
                previous_digest = str(row["version_digest"])

        result: list[OutlinePlanItem] = []
        for row in rows:
            logical_node_id = str(row["logical_node_id"])
            parent_id = row["parent_contract_id"]
            parent_digest = (
                str(by_id[str(parent_id)]["version_digest"])
                if parent_id is not None
                else ""
            )
            result.append(
                OutlinePlanItem(
                    logical_node_id=logical_node_id,
                    version_id=str(row["version_id"]),
                    version_digest=str(row["version_digest"]),
                    parent_logical_node_id=(str(parent_id) if parent_id is not None else None),
                    level=OutlineLevel(str(row["level"])),
                    sibling_index=sibling_indexes[logical_node_id],
                    expansion_state=(
                        "expanded" if logical_node_id in children else "unexpanded"
                    ),
                    validated_parent_digest=parent_digest,
                    validated_previous_sibling_digest=previous_digests.get(
                        logical_node_id, ""
                    ),
                )
            )
        return self._validate_plan_items(novel_id, result)

    def backfill_initial_plan(self, novel_id: str) -> BackfillResult:
        head = self.ensure_planning_head(novel_id)
        if head.active_plan_revision_id:
            return BackfillResult(
                status=BackfillStatus.ALREADY_BACKFILLED,
                head=head,
                plan=self.get_plan_revision(head.active_plan_revision_id),
            )
        conn = self._connection()
        if conn.in_transaction:
            raise OutlineGateError("outline plan backfill requires a clean connection")
        try:
            # The legacy tree is mutable until this book's Head is switched.
            # Derive and seal a shadow snapshot only while the writer lock
            # proves the Head is still legacy and unclaimed.
            conn.execute("BEGIN IMMEDIATE")
            head = self.get_planning_head(novel_id)
            if head.active_plan_revision_id:
                conn.commit()
                return BackfillResult(
                    status=BackfillStatus.ALREADY_BACKFILLED,
                    head=head,
                    plan=self.get_plan_revision(
                        head.active_plan_revision_id,
                        _connection=conn,
                    ),
                )
            if head.authority_mode != PlanningAuthorityMode.LEGACY:
                raise OutlineGateError(
                    "manifest planning Head has no active immutable revision"
                )
            formal_row = conn.execute(
                """
                SELECT 1 FROM chapters
                WHERE novel_id = ? AND trim(COALESCE(content, '')) <> ''
                LIMIT 1
                """,
                (novel_id,),
            ).fetchone()
            if formal_row is not None:
                conn.commit()
                return BackfillResult(
                    status=BackfillStatus.PLANNING_MIGRATION_REQUIRED,
                    head=head,
                    reason="formal history requires canonical reconciliation",
                )
            items = self._legacy_projection_items(novel_id)
        except OutlineGateError as exc:
            if conn.in_transaction:
                conn.rollback()
            return BackfillResult(
                status=BackfillStatus.PLANNING_MIGRATION_REQUIRED,
                head=head,
                reason=str(exc),
            )
        except BaseException:
            if conn.in_transaction:
                conn.rollback()
            raise

        try:
            canonical_prefix_digest = ""
            digest = canonical_plan_digest(
                canonical_prefix_digest=canonical_prefix_digest,
                items=items,
            )
            existing = conn.execute(
                """
                SELECT id FROM outline_plan_revisions
                WHERE novel_id = ? AND digest = ? AND sealed_at IS NOT NULL
                """,
                (novel_id, digest),
            ).fetchone()
            now = self._now()
            if existing is not None:
                plan = self.get_plan_revision(
                    str(existing["id"]), _connection=conn
                )
                activated = conn.execute(
                    """
                    UPDATE outline_planning_heads
                    SET active_plan_revision_id = ?, active_plan_digest = ?,
                        working_plan_revision_id = NULL, updated_at = ?
                    WHERE novel_id = ? AND authority_mode = 'legacy'
                      AND active_plan_revision_id IS NULL
                    """,
                    (plan.id, plan.digest, now, novel_id),
                )
                if activated.rowcount != 1:
                    raise OutlineGateError("legacy planning Head changed during backfill")
                conn.commit()
                updated_head = self.get_planning_head(novel_id)
                return BackfillResult(
                    status=BackfillStatus.MIGRATED,
                    head=updated_head,
                    plan=plan,
                )

            revision = int(
                conn.execute(
                    "SELECT COALESCE(MAX(revision), 0) + 1 AS revision "
                    "FROM outline_plan_revisions WHERE novel_id = ?",
                    (novel_id,),
                ).fetchone()["revision"]
            )
            plan_id = f"outline-plan-{uuid4()}"
            conn.execute(
                """
                INSERT INTO outline_plan_revisions
                    (id, novel_id, revision, status, digest,
                     canonical_prefix_digest, canonical_boundary_json,
                     reconciliation_status, reconciliation_report_json,
                     created_by, created_at, updated_at)
                VALUES (?, ?, ?, 'draft', ?, ?, ?, 'aligned', '{}',
                        'legacy_backfill', ?, ?)
                """,
                (
                    plan_id,
                    novel_id,
                    revision,
                    digest,
                    canonical_prefix_digest,
                    json.dumps({"formal_head": 0}, sort_keys=True),
                    now,
                    now,
                ),
            )
            self._insert_plan_items(conn, plan_id, items, now)
            conn.execute(
                """
                UPDATE outline_contract_versions
                SET sealed_at = ?
                WHERE sealed_at IS NULL AND id IN (
                    SELECT version_id FROM outline_plan_revision_items
                    WHERE plan_revision_id = ?
                )
                """,
                (now, plan_id),
            )
            conn.execute(
                """
                UPDATE outline_plan_revisions
                SET status = 'ready_for_review', sealed_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (now, now, plan_id),
            )
            conn.execute(
                """
                UPDATE outline_planning_heads
                SET active_plan_revision_id = ?, active_plan_digest = ?,
                    working_plan_revision_id = NULL, updated_at = ?
                WHERE novel_id = ? AND authority_mode = 'legacy'
                  AND active_plan_revision_id IS NULL
                """,
                (plan_id, digest, now, novel_id),
            )
            if conn.execute("SELECT changes()").fetchone()[0] != 1:
                raise OutlineGateError("legacy planning Head changed during backfill")
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        plan = self.get_plan_revision(plan_id)
        updated_head = self.get_planning_head(novel_id)
        return BackfillResult(
            status=BackfillStatus.MIGRATED,
            head=updated_head,
            plan=plan,
        )

    @staticmethod
    def _as_mapping(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        return dict(row)

    @staticmethod
    def _revision_from_row(
        row: Optional[sqlite3.Row], contract_row: dict[str, Any]
    ) -> Optional[OutlineContract]:
        if row is None:
            return None
        values = dict(row)
        payload = OutlinePayload.from_dict(json.loads(values["payload_json"] or "{}"))
        return OutlineContract(
            id=str(contract_row["id"]),
            novel_id=str(contract_row["novel_id"]),
            level=OutlineLevel(str(contract_row["level"])),
            revision=int(values["revision"]),
            payload=payload,
            status=OutlineStatus(str(values["status"])),
            parent_id=contract_row.get("parent_contract_id"),
            story_node_id=contract_row.get("story_node_id"),
            parent_revision_digest=str(values["parent_revision_digest"] or ""),
            source=OutlineSource(str(values["source"])),
            author_locked=bool(contract_row.get("author_locked")),
            has_author_edits=bool(contract_row.get("has_author_edits")),
            published_digest=str(values["digest"] or ""),
            previous_sibling_digest=str(values.get("previous_sibling_digest") or ""),
        )

    def _slot_from_row(self, row: sqlite3.Row) -> OutlineContractSlot:
        conn = self._connection()
        contract = self._as_mapping(row)
        active_id = contract.get("active_version_id")
        draft_id = contract.get("draft_version_id")
        active_row = (
            conn.execute("SELECT * FROM outline_contract_versions WHERE id = ?", (active_id,)).fetchone()
            if active_id
            else None
        )
        draft_row = (
            conn.execute("SELECT * FROM outline_contract_versions WHERE id = ?", (draft_id,)).fetchone()
            if draft_id
            else None
        )
        return OutlineContractSlot(
            id=str(contract["id"]),
            novel_id=str(contract["novel_id"]),
            level=OutlineLevel(str(contract["level"])),
            parent_contract_id=contract.get("parent_contract_id"),
            story_node_id=contract.get("story_node_id"),
            active=self._revision_from_row(active_row, contract),
            draft=self._revision_from_row(draft_row, contract),
            author_locked=bool(contract.get("author_locked")),
            has_author_edits=bool(contract.get("has_author_edits")),
        )

    def get_slot(self, contract_id: str) -> OutlineContractSlot:
        row = self._connection().execute(
            "SELECT * FROM outline_contracts WHERE id = ?", (contract_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"outline contract not found: {contract_id}")
        return self._slot_from_row(row)

    def get_slot_by_story_node(
        self, novel_id: str, story_node_id: str
    ) -> Optional[OutlineContractSlot]:
        row = self._connection().execute(
            """
            SELECT * FROM outline_contracts
            WHERE novel_id = ? AND story_node_id = ?
            """,
            (novel_id, story_node_id),
        ).fetchone()
        return self._slot_from_row(row) if row is not None else None

    def ensure_root(self, novel_id: str) -> OutlineContractSlot:
        conn = self._connection()
        self._begin_legacy_mutation_transaction(conn, novel_id, "ensure_root")
        try:
            row = conn.execute(
                """
                SELECT * FROM outline_contracts
                WHERE novel_id = ? AND level = 'outline' AND parent_contract_id IS NULL
                """,
                (novel_id,),
            ).fetchone()
            if row is not None:
                slot = self._slot_from_row(row)
            else:
                contract_id = f"outline-{uuid4()}"
                now = datetime.now().isoformat()
                conn.execute(
                    """
                    INSERT INTO outline_contracts
                        (id, novel_id, level, status, created_at, updated_at)
                    VALUES (?, ?, 'outline', 'draft', ?, ?)
                    """,
                    (contract_id, novel_id, now, now),
                )
                slot = self.get_slot(contract_id)
            conn.commit()
            return slot
        except BaseException:
            conn.rollback()
            raise

    def create_contract(
        self,
        *,
        novel_id: str,
        level: OutlineLevel,
        parent_contract_id: str,
        story_node_id: Optional[str] = None,
    ) -> OutlineContractSlot:
        conn = self._connection()
        self._begin_legacy_mutation_transaction(conn, novel_id, "create_contract")
        try:
            parent = self.get_slot(parent_contract_id)
            if parent.novel_id != novel_id:
                raise OutlineGateError("parent outline belongs to another novel")
            if parent.level.child_level != level:
                expected = parent.level.child_level.value if parent.level.child_level else "none"
                raise OutlineGateError(f"{parent.level.value} can only create {expected}")
            if parent.active is None or parent.active.status != OutlineStatus.SYNCED:
                raise OutlineGateError(
                    f"{parent.level.value}:{parent.active_status.value if parent.active_status else 'missing'} must be synced before generating {level.value}"
                )
            contract_id = f"outline-{uuid4()}"
            now = datetime.now().isoformat()
            conn.execute(
                """
                INSERT INTO outline_contracts
                    (id, novel_id, level, story_node_id, parent_contract_id, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 'draft', ?, ?)
                """,
                (contract_id, novel_id, level.value, story_node_id, parent_contract_id, now, now),
            )
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        return self.get_slot(contract_id)

    def save_draft(
        self,
        contract_id: str,
        payload: OutlinePayload,
        *,
        source: OutlineSource = OutlineSource.AI,
    ) -> OutlineContractSlot:
        slot = self.get_slot(contract_id)
        conn = self._connection()
        self._begin_legacy_mutation_transaction(conn, slot.novel_id, "save_draft")
        try:
            slot = self.get_slot(contract_id)
            parent_digest = ""
            if slot.parent_contract_id:
                parent = self.get_slot(slot.parent_contract_id)
                if parent.active is None or parent.active.status != OutlineStatus.SYNCED:
                    raise OutlineGateError(
                        f"{parent.level.value}:{parent.active_status.value if parent.active_status else 'missing'} must be synced before drafting {slot.level.value}"
                    )
                parent_digest = parent.active.published_digest or parent.active.digest
            row = conn.execute(
                "SELECT COALESCE(MAX(revision), 0) AS current_revision FROM outline_contract_versions WHERE contract_id = ?",
                (contract_id,),
            ).fetchone()
            revision = int(row["current_revision"] or 0) + 1
            version_id = f"outline-version-{uuid4()}"
            now = datetime.now().isoformat()
            payload_json = json.dumps(payload.canonical_dict(), ensure_ascii=False, sort_keys=True)
            conn.execute(
                """
                INSERT INTO outline_contract_versions
                    (id, contract_id, revision, payload_json, digest, parent_revision_digest, source, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'draft', ?, ?)
                """,
                (version_id, contract_id, revision, payload_json, payload.digest, parent_digest, source.value, now, now),
            )
            conn.execute(
                """
                UPDATE outline_contracts
                SET draft_version_id = ?,
                    status = CASE WHEN active_version_id IS NULL THEN 'draft' ELSE status END,
                    has_author_edits = CASE WHEN ? = 'author' THEN 1 ELSE has_author_edits END,
                    updated_at = ?
                WHERE id = ?
                """,
                (version_id, source.value, now, contract_id),
            )
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        return self.get_slot(contract_id)

    def _previous_synced_sibling(self, slot: OutlineContractSlot) -> Optional[OutlineContract]:
        """Return the preceding physical sibling's active plan, if one exists."""

        if not slot.parent_contract_id or not slot.story_node_id:
            return None
        conn = self._connection()
        node = conn.execute(
            "SELECT parent_id, order_index, number FROM story_nodes WHERE id = ?",
            (slot.story_node_id,),
        ).fetchone()
        if node is None:
            return None
        previous = conn.execute(
            """
            SELECT c.*
            FROM story_nodes AS n
            JOIN outline_contracts AS c ON c.story_node_id = n.id AND c.novel_id = ?
            WHERE n.novel_id = ?
              AND COALESCE(n.parent_id, '') = COALESCE(?, '')
              AND n.node_type = ?
              AND (n.order_index < ? OR (n.order_index = ? AND n.number < ?))
            ORDER BY n.order_index DESC, n.number DESC, n.id DESC
            LIMIT 1
            """,
            (
                slot.novel_id,
                slot.novel_id,
                node["parent_id"],
                slot.level.value,
                int(node["order_index"]),
                int(node["order_index"]),
                int(node["number"]),
            ),
        ).fetchone()
        if previous is None:
            return None
        sibling = self._slot_from_row(previous)
        if sibling.active is None or sibling.active.status != OutlineStatus.SYNCED:
            raise OutlineGateError("previous sibling must be synced before publishing this outline")
        return sibling.active

    def previous_synced_sibling(self, contract_id: str) -> Optional[OutlineContract]:
        """Expose the immediate published sibling handoff for draft prompting."""

        return self._previous_synced_sibling(self.get_slot(contract_id))

    @staticmethod
    def _validate_sibling_continuity(
        draft: OutlineContract, previous: Optional[OutlineContract]
    ) -> str:
        if previous is None:
            return ""
        blockers = draft.payload.sibling_continuity_blockers()
        if blockers:
            raise OutlineGateError("sibling continuity requires " + ", ".join(blockers))
        previous_end = previous.payload.chapter_end
        next_start = draft.payload.chapter_start
        if previous_end is not None and next_start is not None and next_start != previous_end + 1:
            raise OutlineGateError(
                f"sibling continuity requires chapter_start {previous_end + 1}, got {next_start}"
            )
        if previous_end is not None and draft.payload.chapter_end is not None:
            if draft.payload.chapter_end < previous_end + 1:
                raise OutlineGateError("sibling continuity requires a forward chapter range")
        return previous.published_digest or previous.digest

    def publish_and_sync(
        self,
        contract_id: str,
        *,
        expected_revision: int,
        idempotency_key: str = "",
        author_locked: Optional[bool] = None,
    ) -> OutlineContractSlot:
        """Atomically make the chosen draft current and regenerate its projection."""

        slot = self.get_slot(contract_id)
        conn = self._connection()
        self._begin_legacy_mutation_transaction(
            conn, slot.novel_id, "publish_and_sync"
        )
        try:
            slot = self.get_slot(contract_id)
            if idempotency_key:
                operation = "publish_and_sync"
                existing = conn.execute(
                    """
                    SELECT contract_id, revision FROM outline_operation_keys
                    WHERE novel_id = ? AND idempotency_key = ? AND operation = ?
                    """,
                    (slot.novel_id, idempotency_key, operation),
                ).fetchone()
                if existing is not None:
                    if str(existing["contract_id"]) != contract_id:
                        raise OutlineGateError(
                            "idempotency key belongs to another outline contract"
                        )
                    conn.commit()
                    return self.get_slot(contract_id)

            draft = slot.draft
            if draft is None:
                raise OutlineGateError("no draft revision is available to publish")
            if draft.revision != expected_revision:
                raise OutlineGateError(
                    f"revision conflict: expected {expected_revision}, current draft is {draft.revision}"
                )
            if slot.parent_contract_id:
                parent = self.get_slot(slot.parent_contract_id)
                if parent.active is None or parent.active.status != OutlineStatus.SYNCED:
                    raise OutlineGateError("parent outline must be synced before publishing a child")
                parent_digest = parent.active.published_digest or parent.active.digest
                if draft.parent_revision_digest != parent_digest:
                    raise OutlineGateError(
                        "parent outline changed; regenerate or reconcile this child draft"
                    )

            previous_sibling_digest = self._validate_sibling_continuity(
                draft, self._previous_synced_sibling(slot)
            )

            now = datetime.now().isoformat()
            locked_value = slot.author_locked if author_locked is None else bool(author_locked)
            active_version_id = self._active_version_id(contract_id) if slot.active else None
            draft_version_id = self._draft_version_id(contract_id)
            draft_sealed = conn.execute(
                "SELECT sealed_at FROM outline_contract_versions WHERE id = ?",
                (draft_version_id,),
            ).fetchone()
            if draft_sealed is None:
                raise OutlineGateError("draft outline version is missing")
            if draft_sealed["sealed_at"]:
                raise OutlineGateError(
                    "sealed outline version cannot be published by legacy flow"
                )
            active_is_sealed = False
            if active_version_id:
                active_row = conn.execute(
                    "SELECT sealed_at FROM outline_contract_versions WHERE id = ?",
                    (active_version_id,),
                ).fetchone()
                if active_row is None:
                    raise OutlineGateError("active outline version is missing")
                active_is_sealed = bool(active_row["sealed_at"])

            if active_version_id is not None and not active_is_sealed:
                conn.execute(
                    "UPDATE outline_contract_versions SET status = 'superseded', updated_at = ? WHERE id = ?",
                    (now, active_version_id),
                )
            conn.execute(
                "UPDATE outline_contract_versions SET status = 'syncing', updated_at = ? WHERE id = ?",
                (now, draft_version_id),
            )
            conn.execute(
                "UPDATE outline_contract_versions SET previous_sibling_digest = ? WHERE id = ?",
                (previous_sibling_digest, draft_version_id),
            )
            conn.execute(
                """
                UPDATE outline_contracts
                SET active_version_id = draft_version_id,
                    draft_version_id = NULL,
                    status = 'syncing',
                    author_locked = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (int(locked_value), now, contract_id),
            )
            active_row = conn.execute(
                "SELECT digest, payload_json FROM outline_contract_versions WHERE id = ?",
                (draft_version_id,),
            ).fetchone()
            conn.execute(
                "UPDATE outline_plan_projections SET is_active = 0 WHERE contract_id = ?", (contract_id,)
            )
            conn.execute(
                """
                INSERT INTO outline_plan_projections
                    (id, novel_id, contract_id, version_id, digest, payload_json, is_active, synced_at, created_at)
                VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
                ON CONFLICT(contract_id, version_id) DO UPDATE SET
                    digest = excluded.digest,
                    payload_json = excluded.payload_json,
                    is_active = 1,
                    synced_at = excluded.synced_at
                """,
                (
                    f"outline-projection-{uuid4()}",
                    slot.novel_id,
                    contract_id,
                    draft_version_id,
                    active_row["digest"],
                    active_row["payload_json"],
                    now,
                    now,
                ),
            )
            conn.execute(
                "UPDATE outline_contract_versions SET status = 'synced', updated_at = ? WHERE id = ?",
                (now, draft_version_id),
            )
            conn.execute(
                "UPDATE outline_contracts SET status = 'synced', updated_at = ? WHERE id = ?",
                (now, contract_id),
            )
            self._invalidate_descendants(conn, contract_id, now)
            if idempotency_key:
                conn.execute(
                    """
                    INSERT INTO outline_operation_keys
                        (novel_id, idempotency_key, operation, contract_id, revision, created_at)
                    VALUES (?, ?, 'publish_and_sync', ?, ?, ?)
                    """,
                    (slot.novel_id, idempotency_key, contract_id, draft.revision, now),
                )
            conn.execute(
                """
                UPDATE outline_planning_heads
                SET active_plan_revision_id = NULL,
                    active_plan_digest = '',
                    updated_at = ?
                WHERE novel_id = ? AND authority_mode = 'legacy'
                  AND active_plan_revision_id IS NOT NULL
                """,
                (now, slot.novel_id),
            )
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        return self.get_slot(contract_id)

    def _active_version_id(self, contract_id: str) -> str:
        row = self._connection().execute(
            "SELECT active_version_id FROM outline_contracts WHERE id = ?", (contract_id,)
        ).fetchone()
        if row is None or not row["active_version_id"]:
            raise OutlineGateError("active outline version is missing")
        return str(row["active_version_id"])

    def _draft_version_id(self, contract_id: str) -> str:
        row = self._connection().execute(
            "SELECT draft_version_id FROM outline_contracts WHERE id = ?", (contract_id,)
        ).fetchone()
        if row is None or not row["draft_version_id"]:
            raise OutlineGateError("draft outline version is missing")
        return str(row["draft_version_id"])

    @staticmethod
    def _invalidate_descendants(conn: sqlite3.Connection, contract_id: str, now: str) -> None:
        rows = conn.execute(
            """
            WITH RECURSIVE descendants(id, author_locked, has_author_edits, active_version_id) AS (
                SELECT id, author_locked, has_author_edits, active_version_id
                FROM outline_contracts WHERE parent_contract_id = ?
                UNION ALL
                SELECT c.id, c.author_locked, c.has_author_edits, c.active_version_id
                FROM outline_contracts AS c
                JOIN descendants AS d ON c.parent_contract_id = d.id
            )
            SELECT * FROM descendants WHERE active_version_id IS NOT NULL
            """,
            (contract_id,),
        ).fetchall()
        for row in rows:
            status = "conflict" if bool(row["author_locked"]) or bool(row["has_author_edits"]) else "stale"
            conn.execute(
                "UPDATE outline_contracts SET status = ?, updated_at = ? WHERE id = ?",
                (status, now, row["id"]),
            )
            conn.execute(
                """
                UPDATE outline_contract_versions
                SET status = ?, updated_at = ?
                WHERE id = ? AND sealed_at IS NULL
                """,
                (status, now, row["active_version_id"]),
            )
            conn.execute(
                "UPDATE outline_plan_projections SET is_active = 0 WHERE contract_id = ?",
                (row["id"],),
            )

    def list_versions(self, contract_id: str) -> list[OutlineContract]:
        slot = self.get_slot(contract_id)
        contract_row = {
            "id": slot.id,
            "novel_id": slot.novel_id,
            "level": slot.level.value,
            "parent_contract_id": slot.parent_contract_id,
            "story_node_id": slot.story_node_id,
            "author_locked": int(slot.author_locked),
            "has_author_edits": int(slot.has_author_edits),
        }
        rows = self._connection().execute(
            "SELECT * FROM outline_contract_versions WHERE contract_id = ? ORDER BY revision",
            (contract_id,),
        ).fetchall()
        return [self._revision_from_row(row, contract_row) for row in rows if row is not None]

    def list_active_projections(self, novel_id: str) -> list[dict[str, Any]]:
        """Context assembly calls this, never draft/history revision queries."""

        rows = self._connection().execute(
            """
            SELECT p.contract_id, c.level, p.version_id, p.digest, p.payload_json
            FROM outline_plan_projections AS p
            JOIN outline_contracts AS c ON c.id = p.contract_id
            WHERE p.novel_id = ? AND p.is_active = 1 AND c.status = 'synced'
            ORDER BY CASE c.level
                WHEN 'outline' THEN 0 WHEN 'part' THEN 1 WHEN 'volume' THEN 2
                WHEN 'act' THEN 3 ELSE 4 END
            """,
            (novel_id,),
        ).fetchall()
        return [
            {
                "contract_id": row["contract_id"],
                "level": row["level"],
                "version_id": row["version_id"],
                "digest": row["digest"],
                "payload": json.loads(row["payload_json"] or "{}"),
            }
            for row in rows
        ]

    def start_generation_attempt(
        self,
        contract_id: str,
        *,
        prompt_snapshot: dict[str, Any],
        context_digest: str,
        retry_of_attempt_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """Create one durable streamed-draft attempt before calling an LLM."""

        slot = self.get_slot(contract_id)
        conn = self._connection()
        self._begin_legacy_mutation_transaction(
            conn, slot.novel_id, "start_generation_attempt"
        )
        try:
            # Re-read the contract and retry source after acquiring the writer
            # lock so a manifest cutover cannot leave a legacy stream behind.
            slot = self.get_slot(contract_id)
            snapshot = dict(prompt_snapshot or {})
            if retry_of_attempt_id:
                previous = self.get_generation_attempt(retry_of_attempt_id)
                if previous["contract_id"] != contract_id:
                    raise OutlineGateError("retry attempt belongs to another outline contract")
                if previous["status"] not in {"failed", "cancelled"}:
                    raise OutlineGateError(
                        "only failed or cancelled outline attempts can be retried"
                    )
                if previous["context_digest"] != context_digest:
                    raise OutlineGateError(
                        "outline context changed; start a new generation attempt"
                    )
                snapshot = dict(previous["prompt_snapshot"])

            attempt_id = f"outline-attempt-{uuid4()}"
            now = self._now()
            conn.execute(
                """
                INSERT INTO outline_generation_attempts
                    (id, contract_id, status, retry_of_attempt_id, context_digest,
                     prompt_snapshot_json, created_at, updated_at)
                VALUES (?, ?, 'running', ?, ?, ?, ?, ?)
                """,
                (
                    attempt_id,
                    contract_id,
                    retry_of_attempt_id,
                    context_digest,
                    json.dumps(snapshot, ensure_ascii=False, sort_keys=True),
                    now,
                    now,
                ),
            )
            self._append_generation_attempt_event(
                attempt_id,
                {
                    "type": "started",
                    "contract_id": contract_id,
                    "retry_of_attempt_id": retry_of_attempt_id,
                },
                commit=False,
            )
            conn.commit()
        except sqlite3.IntegrityError as exc:
            conn.rollback()
            raise OutlineGateError("an outline generation attempt is already running") from exc
        except BaseException:
            conn.rollback()
            raise
        return self.get_generation_attempt(attempt_id)

    def _append_generation_attempt_event(
        self, attempt_id: str, event: dict[str, Any], *, commit: bool = True
    ) -> None:
        conn = self._connection()
        row = conn.execute(
            "SELECT 1 FROM outline_generation_attempts WHERE id = ?", (attempt_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"outline generation attempt not found: {attempt_id}")
        sequence = int(
            conn.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 AS sequence "
                "FROM outline_generation_attempt_events WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()["sequence"]
        )
        conn.execute(
            """
            INSERT INTO outline_generation_attempt_events
                (id, attempt_id, sequence, event_json, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                f"outline-attempt-event-{uuid4()}",
                attempt_id,
                sequence,
                json.dumps(event, ensure_ascii=False, sort_keys=True),
                self._now(),
            ),
        )
        if commit:
            conn.commit()

    def append_generation_attempt_delta(self, attempt_id: str, text: str) -> dict[str, Any]:
        """Persist a streamed delta before it is exposed to a reconnecting client."""

        if not text:
            return self.get_generation_attempt(attempt_id)
        conn = self._connection()
        row = conn.execute(
            """
            SELECT attempt.status, contract.novel_id
            FROM outline_generation_attempts AS attempt
            JOIN outline_contracts AS contract ON contract.id = attempt.contract_id
            WHERE attempt.id = ?
            """,
            (attempt_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"outline generation attempt not found: {attempt_id}")
        self._begin_legacy_mutation_transaction(
            conn, str(row["novel_id"]), "append_generation_attempt_delta"
        )
        try:
            row = conn.execute(
                """
                SELECT attempt.status, contract.novel_id
                FROM outline_generation_attempts AS attempt
                JOIN outline_contracts AS contract ON contract.id = attempt.contract_id
                WHERE attempt.id = ?
                """,
                (attempt_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"outline generation attempt not found: {attempt_id}")
            if row["status"] != "running":
                raise OutlineGateError("outline generation attempt is no longer running")
            conn.execute(
                """
                UPDATE outline_generation_attempts
                SET accumulated_text = accumulated_text || ?, updated_at = ?
                WHERE id = ?
                """,
                (text, self._now(), attempt_id),
            )
            self._append_generation_attempt_event(
                attempt_id,
                {"type": "delta", "text": text},
                commit=False,
            )
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        return self.get_generation_attempt(attempt_id)

    def complete_generation_attempt(
        self, attempt_id: str, *, draft_revision: int
    ) -> dict[str, Any]:
        return self._finish_generation_attempt(
            attempt_id,
            status="completed",
            draft_revision=draft_revision,
        )

    def fail_generation_attempt(self, attempt_id: str, error: str) -> dict[str, Any]:
        return self._finish_generation_attempt(attempt_id, status="failed", error=error)

    def cancel_generation_attempt(self, attempt_id: str) -> dict[str, Any]:
        return self._finish_generation_attempt(attempt_id, status="cancelled")

    def _finish_generation_attempt(
        self,
        attempt_id: str,
        *,
        status: str,
        draft_revision: Optional[int] = None,
        error: str = "",
    ) -> dict[str, Any]:
        if status not in {"completed", "failed", "cancelled"}:
            raise ValueError("invalid outline generation attempt status")
        conn = self._connection()
        row = conn.execute(
            """
            SELECT attempt.status, contract.novel_id
            FROM outline_generation_attempts AS attempt
            JOIN outline_contracts AS contract ON contract.id = attempt.contract_id
            WHERE attempt.id = ?
            """,
            (attempt_id,),
        ).fetchone()
        if row is None:
            raise KeyError(f"outline generation attempt not found: {attempt_id}")
        self._begin_legacy_mutation_transaction(
            conn, str(row["novel_id"]), "finish_generation_attempt"
        )
        try:
            row = conn.execute(
                """
                SELECT attempt.status, contract.novel_id
                FROM outline_generation_attempts AS attempt
                JOIN outline_contracts AS contract ON contract.id = attempt.contract_id
                WHERE attempt.id = ?
                """,
                (attempt_id,),
            ).fetchone()
            if row is None:
                raise KeyError(f"outline generation attempt not found: {attempt_id}")
            if row["status"] != "running":
                conn.commit()
                return self.get_generation_attempt(attempt_id)
            now = self._now()
            event = {
                "type": "completed"
                if status == "completed"
                else ("cancelled" if status == "cancelled" else "error")
            }
            if draft_revision is not None:
                event["draft_revision"] = draft_revision
            if error:
                event["message"] = error
            conn.execute(
                """
                UPDATE outline_generation_attempts
                SET status = ?, draft_revision = ?, error = ?, completed_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (status, draft_revision, error, now, now, attempt_id),
            )
            self._append_generation_attempt_event(attempt_id, event, commit=False)
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        return self.get_generation_attempt(attempt_id)

    def get_generation_attempt(
        self, attempt_id: str, *, after_sequence: int = 0
    ) -> dict[str, Any]:
        conn = self._connection()
        row = conn.execute(
            "SELECT * FROM outline_generation_attempts WHERE id = ?", (attempt_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"outline generation attempt not found: {attempt_id}")
        events = conn.execute(
            """
            SELECT sequence, event_json FROM outline_generation_attempt_events
            WHERE attempt_id = ? AND sequence > ?
            ORDER BY sequence
            """,
            (attempt_id, int(after_sequence)),
        ).fetchall()
        return {
            "id": str(row["id"]),
            "contract_id": str(row["contract_id"]),
            "status": str(row["status"]),
            "retry_of_attempt_id": row["retry_of_attempt_id"],
            "context_digest": str(row["context_digest"] or ""),
            "prompt_snapshot": json.loads(row["prompt_snapshot_json"] or "{}"),
            "accumulated_text": str(row["accumulated_text"] or ""),
            "draft_revision": row["draft_revision"],
            "error": str(row["error"] or ""),
            "events": [
                {"sequence": int(event["sequence"]), **json.loads(event["event_json"] or "{}")}
                for event in events
            ],
        }

    def get_latest_generation_attempt(
        self, contract_id: str, *, after_sequence: int = 0
    ) -> Optional[dict[str, Any]]:
        row = self._connection().execute(
            """
            SELECT id FROM outline_generation_attempts
            WHERE contract_id = ?
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            (contract_id,),
        ).fetchone()
        return self.get_generation_attempt(str(row["id"]), after_sequence=after_sequence) if row else None

    def _require_open_manifest_cohort_attempt(
        self,
        conn: sqlite3.Connection,
        *,
        plan_revision_id: str,
        parent_logical_node_id: str,
        level: OutlineLevel,
        require_unexpanded: bool = True,
    ) -> OutlinePlanRevision:
        plan = self.get_plan_revision(plan_revision_id, _connection=conn)
        head = self.get_planning_head(plan.novel_id)
        if head.authority_mode != PlanningAuthorityMode.MANIFEST:
            raise OutlineGateError("manifest planning authority is required")
        if head.working_plan_revision_id != plan.id:
            raise OutlineGateError("manifest cohort attempt requires the open plan draft")
        if plan.sealed_at or plan.status not in {
            PlanRevisionStatus.DRAFT,
            PlanRevisionStatus.GENERATING,
            PlanRevisionStatus.VALIDATING,
        }:
            raise OutlineGateError("manifest cohort attempt requires an editable draft")
        parent = next(
            (item for item in plan.items if item.logical_node_id == parent_logical_node_id),
            None,
        )
        if parent is None or parent.level.child_level != level:
            raise OutlineGateError("manifest cohort attempt parent does not own this level")
        if require_unexpanded and any(
            item.parent_logical_node_id == parent_logical_node_id for item in plan.items
        ):
            raise OutlineGateError(
                "manifest cohort attempt requires an unexpanded parent; "
                "future replanning must use the impact-closure workflow"
            )
        return plan

    def _append_manifest_cohort_attempt_event(
        self,
        conn: sqlite3.Connection,
        attempt_id: str,
        event: Mapping[str, Any],
    ) -> None:
        sequence = int(
            conn.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 AS sequence "
                "FROM outline_plan_cohort_attempt_events WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()["sequence"]
        )
        conn.execute(
            """
            INSERT INTO outline_plan_cohort_attempt_events
                (id, attempt_id, sequence, event_json, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                f"outline-cohort-attempt-event-{uuid4()}",
                attempt_id,
                sequence,
                json.dumps(dict(event), ensure_ascii=False, sort_keys=True),
                self._now(),
            ),
        )

    def start_manifest_cohort_attempt(
        self,
        *,
        plan_revision_id: str,
        parent_logical_node_id: str,
        level: OutlineLevel,
        scope: Mapping[str, Any],
        context_digest: str,
        prompt_snapshot: Mapping[str, Any],
        retry_of_attempt_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """Start one recoverable LLM attempt for an unexpanded draft cohort."""

        conn = self._connection()
        if conn.in_transaction:
            raise OutlineGateError("manifest cohort attempt requires a clean connection")
        try:
            conn.execute("BEGIN IMMEDIATE")
            plan = self._require_open_manifest_cohort_attempt(
                conn,
                plan_revision_id=plan_revision_id,
                parent_logical_node_id=parent_logical_node_id,
                level=level,
            )
            snapshot = dict(prompt_snapshot or {})
            if retry_of_attempt_id:
                previous = self.get_manifest_cohort_attempt(
                    retry_of_attempt_id, _connection=conn
                )
                if (
                    previous["plan_revision_id"] != plan.id
                    or previous["parent_logical_node_id"] != parent_logical_node_id
                    or previous["level"] != level.value
                ):
                    raise OutlineGateError("cohort retry belongs to another scope")
                if previous["status"] not in {"failed", "cancelled"}:
                    raise OutlineGateError("only failed or cancelled cohort attempts can retry")
                if previous["context_digest"] != context_digest:
                    raise OutlineGateError("cohort context changed; start a new attempt")
                snapshot = dict(previous["prompt_snapshot"])
            attempt_id = f"outline-cohort-attempt-{uuid4()}"
            now = self._now()
            conn.execute(
                """
                INSERT INTO outline_plan_cohort_attempts
                    (id, plan_revision_id, parent_logical_node_id, level, status,
                     retry_of_attempt_id, scope_json, context_digest,
                     prompt_snapshot_json, created_at, updated_at)
                VALUES (?, ?, ?, ?, 'running', ?, ?, ?, ?, ?, ?)
                """,
                (
                    attempt_id,
                    plan.id,
                    parent_logical_node_id,
                    level.value,
                    retry_of_attempt_id,
                    json.dumps(dict(scope or {}), ensure_ascii=False, sort_keys=True),
                    context_digest,
                    json.dumps(snapshot, ensure_ascii=False, sort_keys=True),
                    now,
                    now,
                ),
            )
            self._append_manifest_cohort_attempt_event(
                conn,
                attempt_id,
                {
                    "type": "started",
                    "plan_revision_id": plan.id,
                    "parent_logical_node_id": parent_logical_node_id,
                    "level": level.value,
                    "retry_of_attempt_id": retry_of_attempt_id,
                },
            )
            conn.commit()
        except sqlite3.IntegrityError as exc:
            if conn.in_transaction:
                conn.rollback()
            raise OutlineGateError("a manifest cohort attempt is already running") from exc
        except BaseException:
            if conn.in_transaction:
                conn.rollback()
            raise
        return self.get_manifest_cohort_attempt(attempt_id)

    def append_manifest_cohort_attempt_delta(
        self, attempt_id: str, text: str
    ) -> dict[str, Any]:
        if not text:
            return self.get_manifest_cohort_attempt(attempt_id)
        conn = self._connection()
        if conn.in_transaction:
            raise OutlineGateError("manifest cohort attempt requires a clean connection")
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM outline_plan_cohort_attempts WHERE id = ?", (attempt_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"outline cohort attempt not found: {attempt_id}")
            if str(row["status"]) != "running":
                raise OutlineGateError("manifest cohort attempt is no longer running")
            self._require_open_manifest_cohort_attempt(
                conn,
                plan_revision_id=str(row["plan_revision_id"]),
                parent_logical_node_id=str(row["parent_logical_node_id"]),
                level=OutlineLevel(str(row["level"])),
            )
            conn.execute(
                "UPDATE outline_plan_cohort_attempts "
                "SET accumulated_text = accumulated_text || ?, updated_at = ? WHERE id = ?",
                (text, self._now(), attempt_id),
            )
            self._append_manifest_cohort_attempt_event(
                conn, attempt_id, {"type": "delta", "text": text}
            )
            conn.commit()
        except BaseException:
            if conn.in_transaction:
                conn.rollback()
            raise
        return self.get_manifest_cohort_attempt(attempt_id)

    def _finish_manifest_cohort_attempt(
        self, attempt_id: str, *, status: str, error: str = ""
    ) -> dict[str, Any]:
        if status not in {"completed", "failed", "cancelled"}:
            raise ValueError("invalid manifest cohort attempt status")
        conn = self._connection()
        if conn.in_transaction:
            raise OutlineGateError("manifest cohort attempt requires a clean connection")
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM outline_plan_cohort_attempts WHERE id = ?", (attempt_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"outline cohort attempt not found: {attempt_id}")
            if str(row["status"]) != "running":
                conn.commit()
                return self.get_manifest_cohort_attempt(attempt_id)
            self._require_open_manifest_cohort_attempt(
                conn,
                plan_revision_id=str(row["plan_revision_id"]),
                parent_logical_node_id=str(row["parent_logical_node_id"]),
                level=OutlineLevel(str(row["level"])),
                require_unexpanded=False,
            )
            now = self._now()
            conn.execute(
                "UPDATE outline_plan_cohort_attempts "
                "SET status = ?, error = ?, completed_at = ?, updated_at = ? WHERE id = ?",
                (status, error, now, now, attempt_id),
            )
            event = {"type": "completed" if status == "completed" else status}
            if error:
                event["message"] = error
            self._append_manifest_cohort_attempt_event(conn, attempt_id, event)
            conn.commit()
        except BaseException:
            if conn.in_transaction:
                conn.rollback()
            raise
        return self.get_manifest_cohort_attempt(attempt_id)

    def complete_manifest_cohort_attempt(self, attempt_id: str) -> dict[str, Any]:
        return self._finish_manifest_cohort_attempt(attempt_id, status="completed")

    def fail_manifest_cohort_attempt(self, attempt_id: str, error: str) -> dict[str, Any]:
        return self._finish_manifest_cohort_attempt(attempt_id, status="failed", error=error)

    def cancel_manifest_cohort_attempt(self, attempt_id: str) -> dict[str, Any]:
        return self._finish_manifest_cohort_attempt(attempt_id, status="cancelled")

    def get_manifest_cohort_attempt(
        self,
        attempt_id: str,
        *,
        after_sequence: int = 0,
        _connection: Optional[sqlite3.Connection] = None,
    ) -> dict[str, Any]:
        conn = _connection or self._connection()
        row = conn.execute(
            "SELECT * FROM outline_plan_cohort_attempts WHERE id = ?", (attempt_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"outline cohort attempt not found: {attempt_id}")
        events = conn.execute(
            """
            SELECT sequence, event_json FROM outline_plan_cohort_attempt_events
            WHERE attempt_id = ? AND sequence > ? ORDER BY sequence
            """,
            (attempt_id, int(after_sequence)),
        ).fetchall()
        return {
            "id": str(row["id"]),
            "plan_revision_id": str(row["plan_revision_id"]),
            "parent_logical_node_id": str(row["parent_logical_node_id"]),
            "level": str(row["level"]),
            "status": str(row["status"]),
            "retry_of_attempt_id": row["retry_of_attempt_id"],
            "scope": json.loads(row["scope_json"] or "{}"),
            "context_digest": str(row["context_digest"] or ""),
            "prompt_snapshot": json.loads(row["prompt_snapshot_json"] or "{}"),
            "accumulated_text": str(row["accumulated_text"] or ""),
            "error": str(row["error"] or ""),
            "events": [
                {"sequence": int(event["sequence"]), **json.loads(event["event_json"] or "{}")}
                for event in events
            ],
        }
