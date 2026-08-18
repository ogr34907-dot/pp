"""SQLite storage for draft/published five-level outline contracts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
import logging
import sqlite3
from typing import Any, Callable, Mapping, Optional, Sequence, Union
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


logger = logging.getLogger(__name__)


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

    def _validate_plan_projection_bindings(
        self,
        conn: sqlite3.Connection,
        plan: OutlinePlanRevision,
        *,
        allow_unmaterialized: bool = False,
    ) -> dict[str, sqlite3.Row]:
        """Validate frozen mappings, optionally before their StoryNodes are materialized."""

        try:
            rows = conn.execute(
                """
                SELECT item.id AS item_id, item.logical_node_id,
                       item.parent_logical_node_id, item.level,
                       item.expansion_state, item.is_reused,
                       binding.plan_revision_item_id AS binding_item_id,
                       binding.story_node_id, binding.parent_story_node_id,
                       binding.number, binding.order_index,
                       node.id AS live_story_node_id,
                       node.novel_id AS live_novel_id,
                       node.parent_id AS live_parent_story_node_id,
                       node.node_type AS live_node_type,
                       node.number AS live_number,
                       node.order_index AS live_order_index
                FROM outline_plan_revision_items AS item
                LEFT JOIN outline_plan_projection_bindings AS binding
                  ON binding.plan_revision_item_id = item.id
                LEFT JOIN story_nodes AS node ON node.id = binding.story_node_id
                WHERE item.plan_revision_id = ?
                ORDER BY CASE item.level
                    WHEN 'outline' THEN 0 WHEN 'part' THEN 1 WHEN 'volume' THEN 2
                    WHEN 'act' THEN 3 ELSE 4 END,
                    COALESCE(item.parent_logical_node_id, ''), item.sibling_index,
                    item.logical_node_id
                """,
                (plan.id,),
            ).fetchall()
        except sqlite3.OperationalError as exc:
            raise OutlineGateError(
                "active manifest projection binding storage is unavailable"
            ) from exc

        expected_item_ids = {item.id for item in plan.items}
        if not expected_item_ids or len(rows) != len(expected_item_ids):
            raise OutlineGateError("active manifest projection binding set is incomplete")

        by_logical_id: dict[str, sqlite3.Row] = {}
        physical_by_logical_id: dict[str, Optional[str]] = {}
        physical_ids: set[str] = set()
        for row in rows:
            item_id = str(row["item_id"] or "")
            logical_node_id = str(row["logical_node_id"] or "")
            level = str(row["level"] or "")
            if (
                item_id not in expected_item_ids
                or not logical_node_id
                or logical_node_id in by_logical_id
                or row["binding_item_id"] is None
            ):
                raise OutlineGateError("active manifest projection binding set is incomplete")

            binding_values = (
                row["story_node_id"],
                row["parent_story_node_id"],
                row["number"],
                row["order_index"],
            )
            if level == OutlineLevel.OUTLINE.value:
                if any(value is not None for value in binding_values):
                    raise OutlineGateError(
                        "active manifest outline root projection binding must be unbound"
                    )
                physical_by_logical_id[logical_node_id] = None
                by_logical_id[logical_node_id] = row
                continue

            story_node_id = row["story_node_id"]
            if story_node_id is None:
                if any(value is not None for value in binding_values):
                    raise OutlineGateError(
                        "active manifest unbound projection binding is malformed"
                    )
                if str(row["expansion_state"] or "") != "unexpanded":
                    raise OutlineGateError(
                        "active manifest expanded item has no physical projection binding"
                    )
                physical_by_logical_id[logical_node_id] = None
                by_logical_id[logical_node_id] = row
                continue

            if row["number"] is None or row["order_index"] is None:
                raise OutlineGateError(
                    "active manifest physical projection binding is incomplete"
                )
            if row["live_story_node_id"] is None:
                if not allow_unmaterialized or bool(row["is_reused"]):
                    raise OutlineGateError(
                        "active manifest physical projection binding StoryNode is missing"
                    )
            if row["live_story_node_id"] is not None:
                try:
                    number_matches = int(row["live_number"]) == int(row["number"])
                    order_matches = int(row["live_order_index"]) == int(
                        row["order_index"]
                    )
                except (TypeError, ValueError) as exc:
                    raise OutlineGateError(
                        "active manifest physical projection binding is malformed"
                    ) from exc
                if (
                    str(row["live_novel_id"] or "") != plan.novel_id
                    or str(row["live_node_type"] or "") != level
                    or row["live_parent_story_node_id"]
                    != row["parent_story_node_id"]
                    or not number_matches
                    or not order_matches
                ):
                    raise OutlineGateError(
                        "active manifest physical projection binding does not match StoryNode"
                    )

            parent_logical_node_id = row["parent_logical_node_id"]
            if not parent_logical_node_id:
                raise OutlineGateError(
                    "active manifest physical projection binding has no logical parent"
                )
            parent_key = str(parent_logical_node_id)
            if parent_key not in physical_by_logical_id:
                raise OutlineGateError(
                    "active manifest projection binding parent is missing"
                )
            expected_parent_story_node_id = physical_by_logical_id[parent_key]
            if row["parent_story_node_id"] != expected_parent_story_node_id:
                raise OutlineGateError(
                    "active manifest physical projection binding parent does not match"
                )
            if (
                level != OutlineLevel.PART.value
                and expected_parent_story_node_id is None
            ):
                raise OutlineGateError(
                    "active manifest physical projection binding has an unbound parent"
                )
            physical_id = str(story_node_id)
            if physical_id in physical_ids:
                raise OutlineGateError(
                    "active manifest has duplicate physical projection bindings"
                )
            physical_ids.add(physical_id)
            physical_by_logical_id[logical_node_id] = physical_id
            by_logical_id[logical_node_id] = row

        if set(by_logical_id) != {item.logical_node_id for item in plan.items}:
            raise OutlineGateError("active manifest projection binding set is incomplete")
        return by_logical_id

    def _snapshot_plan_projection_bindings(
        self,
        conn: sqlite3.Connection,
        plan: OutlinePlanRevision,
    ) -> None:
        """Freeze the locked plan's current real StoryNode projection once."""

        existing = conn.execute(
            """
            SELECT COUNT(*) AS count
            FROM outline_plan_projection_bindings AS binding
            JOIN outline_plan_revision_items AS item
              ON item.id = binding.plan_revision_item_id
            WHERE item.plan_revision_id = ?
            """,
            (plan.id,),
        ).fetchone()
        if existing is None:
            raise OutlineGateError("outline plan projection binding storage is unavailable")
        existing_count = int(existing["count"] or 0)
        if existing_count == len(plan.items):
            self._validate_plan_projection_bindings(
                conn, plan, allow_unmaterialized=True
            )
            return
        if existing_count != 0:
            raise OutlineGateError("outline plan projection binding snapshot is incomplete")

        rows = conn.execute(
            """
            SELECT item.id AS item_id, item.level, item.expansion_state,
                   contract.story_node_id AS source_story_node_id,
                   node.id AS live_story_node_id,
                   node.parent_id AS live_parent_story_node_id,
                   node.number AS live_number,
                   node.order_index AS live_order_index
            FROM outline_plan_revision_items AS item
            JOIN outline_contracts AS contract
              ON contract.id = item.logical_node_id
            LEFT JOIN story_nodes AS node ON node.id = contract.story_node_id
            WHERE item.plan_revision_id = ?
            """,
            (plan.id,),
        ).fetchall()
        expected_item_ids = {item.id for item in plan.items}
        if len(rows) != len(expected_item_ids):
            raise OutlineGateError("outline plan item set changed during binding snapshot")

        physical_ids: set[str] = set()
        for row in rows:
            item_id = str(row["item_id"] or "")
            if item_id not in expected_item_ids:
                raise OutlineGateError("outline plan item set changed during binding snapshot")
            if str(row["level"] or "") == OutlineLevel.OUTLINE.value:
                values = (None, None, None, None)
            elif row["live_story_node_id"] is None:
                if str(row["expansion_state"] or "") != "unexpanded":
                    raise OutlineGateError(
                        "expanded outline plan item has no physical StoryNode"
                    )
                values = (None, None, None, None)
            else:
                if row["live_number"] is None or row["live_order_index"] is None:
                    raise OutlineGateError(
                        "physical StoryNode has incomplete projection coordinates"
                    )
                story_node_id = str(row["live_story_node_id"])
                if story_node_id in physical_ids:
                    raise OutlineGateError(
                        "outline plan has duplicate physical StoryNode bindings"
                    )
                physical_ids.add(story_node_id)
                values = (
                    story_node_id,
                    row["live_parent_story_node_id"],
                    int(row["live_number"]),
                    int(row["live_order_index"]),
                )
            conn.execute(
                """
                INSERT INTO outline_plan_projection_bindings
                    (plan_revision_item_id, story_node_id, parent_story_node_id,
                     number, order_index)
                VALUES (?, ?, ?, ?, ?)
                """,
                (item_id, *values),
            )

        self._validate_plan_projection_bindings(conn, plan, allow_unmaterialized=True)

    def _copy_plan_projection_bindings(
        self,
        conn: sqlite3.Connection,
        *,
        source_plan_revision_id: str,
        target_plan_revision_id: str,
        expected_item_count: int,
    ) -> None:
        """Copy a sealed plan's physical declaration into its editable clone."""

        copied = conn.execute(
            """
            INSERT INTO outline_plan_projection_bindings
                (plan_revision_item_id, story_node_id, parent_story_node_id,
                 number, order_index)
            SELECT target_item.id, source_binding.story_node_id,
                   source_binding.parent_story_node_id, source_binding.number,
                   source_binding.order_index
            FROM outline_plan_revision_items AS source_item
            JOIN outline_plan_projection_bindings AS source_binding
              ON source_binding.plan_revision_item_id = source_item.id
            JOIN outline_plan_revision_items AS target_item
              ON target_item.plan_revision_id = ?
             AND target_item.logical_node_id = source_item.logical_node_id
            WHERE source_item.plan_revision_id = ?
            """,
            (target_plan_revision_id, source_plan_revision_id),
        )
        if copied.rowcount != expected_item_count:
            raise OutlineGateError("active outline plan projection bindings are incomplete")

    @staticmethod
    def _declare_draft_projection_bindings(
        conn: sqlite3.Connection,
        *,
        plan_revision_id: str,
        parent: OutlinePlanItem,
        children: Sequence[OutlinePlanItem],
    ) -> None:
        """Give new draft children an immutable physical projection declaration."""

        parent_binding = conn.execute(
            """
            SELECT binding.story_node_id
            FROM outline_plan_revision_items AS item
            JOIN outline_plan_projection_bindings AS binding
              ON binding.plan_revision_item_id = item.id
            WHERE item.plan_revision_id = ? AND item.logical_node_id = ?
            """,
            (plan_revision_id, parent.logical_node_id),
        ).fetchone()
        if parent_binding is None:
            raise OutlineGateError("manifest cohort parent has no projection binding")
        parent_story_node_id = parent_binding["story_node_id"]
        if parent.level != OutlineLevel.OUTLINE and parent_story_node_id is None:
            raise OutlineGateError("manifest cohort parent is not physically projected")

        for child in children:
            if not child.id:
                raise OutlineGateError("manifest cohort child has no plan item identity")
            number = child.sibling_index + 1
            if child.level == OutlineLevel.CHAPTER:
                version = conn.execute(
                    "SELECT payload_json FROM outline_contract_versions WHERE id = ?",
                    (child.version_id,),
                ).fetchone()
                if version is None:
                    raise OutlineGateError(
                        "manifest chapter cohort child has no contract version"
                    )
                try:
                    chapter_start = OutlinePayload.from_dict(
                        json.loads(str(version["payload_json"] or "{}"))
                    ).chapter_start
                except (TypeError, ValueError, json.JSONDecodeError) as exc:
                    raise OutlineGateError(
                        "manifest chapter cohort payload is invalid"
                    ) from exc
                if chapter_start is None or int(chapter_start) < 1:
                    raise OutlineGateError(
                        "manifest chapter cohort child has no chapter number"
                    )
                number = int(chapter_start)
            conn.execute(
                """
                INSERT INTO outline_plan_projection_bindings
                    (plan_revision_item_id, story_node_id, parent_story_node_id,
                     number, order_index)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    child.id,
                    f"manifest-node-{uuid4()}",
                    parent_story_node_id,
                    number,
                    child.sibling_index,
                ),
            )

    def projection_bindings_for_revision(
        self,
        plan_revision_id: str,
        *,
        _connection: Optional[sqlite3.Connection] = None,
    ) -> list[dict[str, Any]]:
        """Return one revision's immutable projection declarations.

        Binding rows deliberately point at plan items instead of duplicating
        logical/version identity.  Joining the sealed item and version here
        makes that identity explicit for consumers without trusting mutable
        contract cache fields.
        """

        conn = _connection or self._connection()
        plan = self.get_plan_revision(plan_revision_id, _connection=conn)
        self._validate_plan_projection_bindings(
            conn, plan, allow_unmaterialized=True
        )
        rows = conn.execute(
            """
            SELECT item.id AS plan_revision_item_id, item.plan_revision_id,
                   item.logical_node_id, item.parent_logical_node_id,
                   item.level, item.sibling_index, item.expansion_state,
                   item.version_id, version.digest AS version_digest,
                   binding.story_node_id, binding.parent_story_node_id,
                   binding.number, binding.order_index
            FROM outline_plan_revision_items AS item
            JOIN outline_contract_versions AS version ON version.id = item.version_id
            JOIN outline_plan_projection_bindings AS binding
              ON binding.plan_revision_item_id = item.id
            WHERE item.plan_revision_id = ?
            ORDER BY CASE item.level
                WHEN 'outline' THEN 0 WHEN 'part' THEN 1 WHEN 'volume' THEN 2
                WHEN 'act' THEN 3 ELSE 4 END,
                COALESCE(item.parent_logical_node_id, ''), item.sibling_index,
                item.logical_node_id
            """,
            (plan.id,),
        ).fetchall()
        if len(rows) != len(plan.items):
            raise OutlineGateError("outline plan projection binding set is incomplete")
        return [dict(row) for row in rows]

    def validate_projection_bindings(
        self,
        plan_revision_id: str,
        conn: sqlite3.Connection,
    ) -> dict[str, sqlite3.Row]:
        """Strictly validate a revision's declarations against live projection.

        This is intentionally stricter than the seal-time declaration check:
        callers that need to materialize a sealed draft use the private
        declaration path while this public verifier never treats a missing
        bound StoryNode as valid.
        """

        plan = self.get_plan_revision(plan_revision_id, _connection=conn)
        return self._validate_plan_projection_bindings(conn, plan)

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
        self._validate_plan_projection_bindings(conn, plan)
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

    def active_plan_items_with_payload(
        self,
        novel_id: str,
        *,
        _connection: Optional[sqlite3.Connection] = None,
    ) -> list[dict[str, Any]]:
        """Return the immutable active manifest topology and sealed payloads."""

        conn = _connection or self._connection()
        plan = self._require_active_manifest_plan(novel_id, conn=conn)
        rows = conn.execute(
            """
            SELECT item.id AS item_id, item.logical_node_id,
                   item.parent_logical_node_id, item.level, item.sibling_index,
                   item.expansion_state, item.validated_parent_digest,
                   item.validated_previous_sibling_digest, item.is_reused,
                   contract.novel_id, binding.story_node_id AS story_node_id,
                   binding.parent_story_node_id,
                   binding.number AS story_node_number,
                   binding.order_index AS story_node_order_index,
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
            JOIN outline_plan_projection_bindings AS binding
              ON binding.plan_revision_item_id = item.id
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

    def working_plan_items_with_payload(
        self,
        novel_id: str,
        *,
        _connection: Optional[sqlite3.Connection] = None,
    ) -> list[dict[str, Any]]:
        """Return the open Manifest draft without changing Active semantics."""

        conn = _connection or self._connection()
        head_row = conn.execute(
            "SELECT * FROM outline_planning_heads WHERE novel_id = ?",
            (novel_id,),
        ).fetchone()
        if head_row is None:
            raise KeyError(f"outline planning head not found: {novel_id}")
        head = self._head_from_row(head_row)
        if head.authority_mode != PlanningAuthorityMode.MANIFEST:
            raise OutlineGateError("working outline tree requires manifest planning authority")
        if not head.working_plan_revision_id:
            return []
        plan = self.get_plan_revision(head.working_plan_revision_id, _connection=conn)
        if plan.novel_id != novel_id:
            raise OutlineGateError("working outline plan belongs to another novel")
        if plan.sealed_at or plan.status not in {
            PlanRevisionStatus.DRAFT,
            PlanRevisionStatus.GENERATING,
            PlanRevisionStatus.VALIDATING,
        }:
            raise OutlineGateError("working outline plan is not editable")
        self._validate_plan_projection_bindings(conn, plan, allow_unmaterialized=True)
        rows = conn.execute(
            """
            SELECT item.id AS item_id, item.logical_node_id,
                   item.parent_logical_node_id, item.level, item.sibling_index,
                   item.expansion_state, item.validated_parent_digest,
                   item.validated_previous_sibling_digest,
                   contract.novel_id, binding.story_node_id,
                   binding.parent_story_node_id, binding.number,
                   binding.order_index, version.id AS version_id,
                   version.revision AS version_revision, version.digest AS version_digest,
                   version.payload_json, version.source AS version_source,
                   version.status AS version_status,
                    (SELECT attempt.id
                     FROM outline_plan_cohort_attempts AS attempt
                     WHERE attempt.plan_revision_id = item.plan_revision_id
                       AND attempt.parent_logical_node_id = item.parent_logical_node_id
                       AND attempt.level = item.level
                       AND attempt.status = 'completed'
                     ORDER BY attempt.created_at DESC, attempt.id DESC
                     LIMIT 1) AS cohort_attempt_id,
                    (SELECT attempt.id
                     FROM outline_plan_cohort_attempts AS attempt
                     WHERE attempt.plan_revision_id = item.plan_revision_id
                       AND attempt.parent_logical_node_id = item.logical_node_id
                       AND attempt.level = CASE item.level
                           WHEN 'outline' THEN 'part'
                           WHEN 'part' THEN 'volume'
                           WHEN 'volume' THEN 'act'
                           WHEN 'act' THEN 'chapter'
                           ELSE ''
                       END
                     ORDER BY attempt.created_at DESC, attempt.id DESC
                     LIMIT 1) AS latest_cohort_attempt_id,
                    (SELECT attempt.status
                     FROM outline_plan_cohort_attempts AS attempt
                     WHERE attempt.plan_revision_id = item.plan_revision_id
                       AND attempt.parent_logical_node_id = item.logical_node_id
                       AND attempt.level = CASE item.level
                           WHEN 'outline' THEN 'part'
                           WHEN 'part' THEN 'volume'
                           WHEN 'volume' THEN 'act'
                           WHEN 'act' THEN 'chapter'
                           ELSE ''
                       END
                     ORDER BY attempt.created_at DESC, attempt.id DESC
                     LIMIT 1) AS latest_cohort_attempt_status,
                    (SELECT attempt.error
                     FROM outline_plan_cohort_attempts AS attempt
                     WHERE attempt.plan_revision_id = item.plan_revision_id
                       AND attempt.parent_logical_node_id = item.logical_node_id
                       AND attempt.level = CASE item.level
                           WHEN 'outline' THEN 'part'
                           WHEN 'part' THEN 'volume'
                           WHEN 'volume' THEN 'act'
                           WHEN 'act' THEN 'chapter'
                           ELSE ''
                       END
                     ORDER BY attempt.created_at DESC, attempt.id DESC
                     LIMIT 1) AS latest_cohort_attempt_error,
                    (SELECT attempt.retry_of_attempt_id
                     FROM outline_plan_cohort_attempts AS attempt
                     WHERE attempt.plan_revision_id = item.plan_revision_id
                       AND attempt.parent_logical_node_id = item.logical_node_id
                       AND attempt.level = CASE item.level
                           WHEN 'outline' THEN 'part'
                           WHEN 'part' THEN 'volume'
                           WHEN 'volume' THEN 'act'
                           WHEN 'act' THEN 'chapter'
                           ELSE ''
                       END
                     ORDER BY attempt.created_at DESC, attempt.id DESC
                     LIMIT 1) AS latest_cohort_attempt_retry_of_id,
                    (SELECT attempt.level
                     FROM outline_plan_cohort_attempts AS attempt
                     WHERE attempt.plan_revision_id = item.plan_revision_id
                       AND attempt.parent_logical_node_id = item.logical_node_id
                       AND attempt.level = CASE item.level
                           WHEN 'outline' THEN 'part'
                           WHEN 'part' THEN 'volume'
                           WHEN 'volume' THEN 'act'
                           WHEN 'act' THEN 'chapter'
                           ELSE ''
                       END
                     ORDER BY attempt.created_at DESC, attempt.id DESC
                     LIMIT 1) AS latest_cohort_attempt_level
            FROM outline_plan_revision_items AS item
            JOIN outline_contracts AS contract
              ON contract.id = item.logical_node_id AND contract.novel_id = ?
            JOIN outline_contract_versions AS version
              ON version.id = item.version_id
            JOIN outline_plan_projection_bindings AS binding
              ON binding.plan_revision_item_id = item.id
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
            raise OutlineGateError("working outline item set is incomplete")
        result: list[dict[str, Any]] = []
        for row in rows:
            try:
                payload = OutlinePayload.from_dict(
                    json.loads(str(row["payload_json"] or "{}"))
                )
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise OutlineGateError("working outline payload is invalid") from exc
            story_node_id = (
                str(row["story_node_id"])
                if row["story_node_id"] is not None
                else None
            )
            latest_cohort_attempt = None
            if row["latest_cohort_attempt_id"] is not None:
                latest_cohort_attempt = {
                    "id": str(row["latest_cohort_attempt_id"]),
                    "status": str(row["latest_cohort_attempt_status"] or ""),
                    "error": str(row["latest_cohort_attempt_error"] or ""),
                    "retry_of_attempt_id": row["latest_cohort_attempt_retry_of_id"],
                    "level": str(row["latest_cohort_attempt_level"] or ""),
                }
            result.append(
                {
                    "id": story_node_id or str(row["logical_node_id"]),
                    "item_id": str(row["item_id"]),
                    "logical_node_id": str(row["logical_node_id"]),
                    "tree_mode": "MANIFEST_WORKING",
                    "novel_id": str(row["novel_id"]),
                    "story_node_id": story_node_id,
                    "parent_story_node_id": row["parent_story_node_id"],
                    "parent_logical_node_id": row["parent_logical_node_id"],
                    "node_type": str(row["level"]),
                    "level": str(row["level"]),
                    "number": int(row["number"])
                    if row["number"] is not None
                    else int(row["sibling_index"] or 0) + 1,
                    "order_index": int(row["order_index"])
                    if row["order_index"] is not None
                    else int(row["sibling_index"] or 0),
                    "sibling_index": int(row["sibling_index"] or 0),
                    "expansion_state": str(row["expansion_state"] or "unexpanded"),
                    "title": payload.title,
                    "description": payload.narrative_text,
                    "outline": payload.narrative_text,
                    "chapter_start": payload.chapter_start,
                    "chapter_end": payload.chapter_end,
                    "plan_revision_id": plan.id,
                    "plan_digest": plan.digest,
                    "version_id": str(row["version_id"]),
                    "version_revision": int(row["version_revision"] or 0),
                    "version_digest": str(row["version_digest"] or ""),
                    "version_source": str(row["version_source"] or "ai"),
                    "cohort_attempt_id": row["cohort_attempt_id"],
                    "latest_cohort_attempt": latest_cohort_attempt,
                    "payload": payload.canonical_dict(),
                    "status": "draft",
                    "outline_contract": {
                        "contract_id": str(row["logical_node_id"]),
                        "level": str(row["level"]),
                        "status": "draft",
                        "version_digest": str(row["version_digest"] or ""),
                        "draft_revision": int(row["version_revision"] or 0),
                    },
                }
            )
        return result

    def update_working_plan_item(
        self,
        *,
        plan_revision_id: str,
        logical_node_id: str,
        payload: OutlinePayload,
        expected_plan_digest: str,
        expected_version_digest: str,
        source: OutlineSource = OutlineSource.AUTHOR,
    ) -> OutlinePlanRevision:
        """Edit one leaf in the open Manifest draft using a single CAS transaction."""

        conn = self._connection()
        if conn.in_transaction:
            raise OutlineGateError("working outline item edit requires a clean connection")
        try:
            conn.execute("BEGIN IMMEDIATE")
            head_row = conn.execute(
                "SELECT * FROM outline_planning_heads WHERE novel_id = ("
                "SELECT novel_id FROM outline_plan_revisions WHERE id = ?)",
                (plan_revision_id,),
            ).fetchone()
            if head_row is None:
                raise KeyError(f"outline plan revision not found: {plan_revision_id}")
            head = self._head_from_row(head_row)
            if head.authority_mode != PlanningAuthorityMode.MANIFEST:
                raise OutlineGateError("working outline item edit requires manifest planning authority")
            if head.working_plan_revision_id != plan_revision_id:
                raise OutlineGateError("working outline plan is no longer current")
            plan = self.get_plan_revision(plan_revision_id, _connection=conn)
            if plan.sealed_at or plan.status not in {
                PlanRevisionStatus.DRAFT,
                PlanRevisionStatus.GENERATING,
                PlanRevisionStatus.VALIDATING,
            }:
                raise OutlineGateError("working outline plan is not editable")
            if expected_plan_digest != plan.digest:
                raise OutlineGateError("working outline plan digest changed")
            item = next(
                (candidate for candidate in plan.items if candidate.logical_node_id == logical_node_id),
                None,
            )
            if item is None:
                raise KeyError(f"working outline item not found: {logical_node_id}")
            child = conn.execute(
                "SELECT 1 FROM outline_plan_revision_items "
                "WHERE plan_revision_id = ? AND parent_logical_node_id = ? LIMIT 1",
                (plan.id, logical_node_id),
            ).fetchone()
            if child is not None:
                raise OutlineGateError(
                    "working outline item has direct children; use the impact-closure workflow"
                )
            version_row = conn.execute(
                "SELECT contract_id, revision, digest, payload_json, sealed_at "
                "FROM outline_contract_versions WHERE id = ? AND contract_id = ?",
                (item.version_id, logical_node_id),
            ).fetchone()
            if version_row is None:
                raise OutlineGateError("working outline item version is missing")
            if str(version_row["digest"] or "") != expected_version_digest:
                raise OutlineGateError("working outline item version digest changed")
            revision = int(
                conn.execute(
                    "SELECT COALESCE(MAX(revision), 0) + 1 FROM outline_contract_versions "
                    "WHERE contract_id = ?",
                    (logical_node_id,),
                ).fetchone()[0]
            )
            version_id = f"outline-version-{uuid4()}"
            now = self._now()
            conn.execute(
                """
                INSERT INTO outline_contract_versions
                    (id, contract_id, revision, payload_json, digest,
                     parent_revision_digest, previous_sibling_digest, source,
                     status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'draft', ?, ?)
                """,
                (
                    version_id,
                    logical_node_id,
                    revision,
                    json.dumps(payload.canonical_dict(), ensure_ascii=False, sort_keys=True),
                    payload.digest,
                    str(version_row["digest"] or ""),
                    item.validated_previous_sibling_digest,
                    source.value,
                    now,
                    now,
                ),
            )
            updated_item = OutlinePlanItem(
                id=item.id,
                logical_node_id=item.logical_node_id,
                version_id=version_id,
                version_digest=payload.digest,
                level=item.level,
                sibling_index=item.sibling_index,
                parent_logical_node_id=item.parent_logical_node_id,
                expansion_state=item.expansion_state,
                validated_parent_digest=item.validated_parent_digest,
                validated_previous_sibling_digest=item.validated_previous_sibling_digest,
                is_reused=item.is_reused,
            )
            next_sibling = next(
                (
                    candidate
                    for candidate in plan.items
                    if candidate.parent_logical_node_id == item.parent_logical_node_id
                    and candidate.level == item.level
                    and candidate.sibling_index == item.sibling_index + 1
                ),
                None,
            )
            updated_next_sibling = (
                OutlinePlanItem(
                    id=next_sibling.id,
                    logical_node_id=next_sibling.logical_node_id,
                    version_id=next_sibling.version_id,
                    version_digest=next_sibling.version_digest,
                    level=next_sibling.level,
                    sibling_index=next_sibling.sibling_index,
                    parent_logical_node_id=next_sibling.parent_logical_node_id,
                    expansion_state=next_sibling.expansion_state,
                    validated_parent_digest=next_sibling.validated_parent_digest,
                    validated_previous_sibling_digest=payload.digest,
                    is_reused=next_sibling.is_reused,
                )
                if next_sibling is not None
                else None
            )
            updated_items = tuple(
                updated_item
                if candidate.logical_node_id == logical_node_id
                else (
                    updated_next_sibling
                    if updated_next_sibling is not None
                    and candidate.logical_node_id == updated_next_sibling.logical_node_id
                    else candidate
                )
                for candidate in plan.items
            )
            normalized = self._validate_plan_items(plan.novel_id, updated_items, conn=conn)
            digest = canonical_plan_digest(
                canonical_prefix_digest=plan.canonical_prefix_digest,
                items=normalized,
            )
            item_update = conn.execute(
                """
                UPDATE outline_plan_revision_items
                SET version_id = ?, validated_previous_sibling_digest = ?
                WHERE id = ? AND plan_revision_id = ? AND version_id = ?
                """,
                (
                    version_id,
                    item.validated_previous_sibling_digest,
                    item.id,
                    plan.id,
                    item.version_id,
                ),
            )
            if item_update.rowcount != 1:
                raise OutlineGateError("working outline item changed during edit")
            if updated_next_sibling is not None:
                next_update = conn.execute(
                    """
                    UPDATE outline_plan_revision_items
                    SET validated_previous_sibling_digest = ?
                    WHERE id = ? AND plan_revision_id = ?
                    """,
                    (
                        payload.digest,
                        updated_next_sibling.id,
                        plan.id,
                    ),
                )
                if next_update.rowcount != 1:
                    raise OutlineGateError("working sibling changed during edit")
            plan_update = conn.execute(
                """
                UPDATE outline_plan_revisions
                SET digest = ?, updated_at = ?
                WHERE id = ? AND novel_id = ? AND sealed_at IS NULL
                  AND status = ? AND digest = ?
                """,
                (digest, now, plan.id, plan.novel_id, plan.status.value, plan.digest),
            )
            if plan_update.rowcount != 1:
                raise OutlineGateError("working outline plan changed during edit")
            conn.execute(
                """
                UPDATE outline_contracts
                SET draft_version_id = ?, has_author_edits = CASE WHEN ? = 'author'
                    THEN 1 ELSE has_author_edits END, updated_at = ?
                WHERE id = ? AND novel_id = ?
                """,
                (version_id, source.value, now, logical_node_id, plan.novel_id),
            )
            conn.commit()
        except BaseException:
            if conn.in_transaction:
                conn.rollback()
            raise
        return self.get_plan_revision(plan_revision_id)

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
        canonical_prefix_digest: Optional[str] = None,
        canonical_boundary: Optional[Mapping[str, Any]] = None,
        canonical_boundary_resolver: Optional[
            Callable[[sqlite3.Connection], tuple[str, Mapping[str, Any]]]
        ] = None,
        recover_stale_pristine_draft: bool = False,
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
            active = self._require_active_manifest_plan(novel_id, conn=conn)
            if canonical_boundary_resolver is not None:
                resolved_digest, resolved_boundary = canonical_boundary_resolver(conn)
                canonical_prefix_digest = str(resolved_digest)
                canonical_boundary = dict(resolved_boundary)
            cloned_prefix_digest = (
                active.canonical_prefix_digest
                if canonical_prefix_digest is None
                else str(canonical_prefix_digest)
            )
            cloned_boundary = (
                dict(active.canonical_boundary or {})
                if canonical_boundary is None
                else dict(canonical_boundary)
            )
            if head.working_plan_revision_id:
                if not recover_stale_pristine_draft:
                    raise OutlineGateError("an outline plan draft is already open")
                working = self.get_plan_revision(
                    head.working_plan_revision_id, _connection=conn
                )
                tracks_active = (
                    working.novel_id == novel_id
                    and working.parent_plan_revision_id == active.id
                    and working.base_plan_digest == active.digest
                    and working.sealed_at is None
                    and working.status
                    in {
                        PlanRevisionStatus.DRAFT,
                        PlanRevisionStatus.GENERATING,
                        PlanRevisionStatus.VALIDATING,
                    }
                )
                if (
                    tracks_active
                    and working.canonical_prefix_digest == cloned_prefix_digest
                    and dict(working.canonical_boundary or {}) == cloned_boundary
                ):
                    conn.commit()
                    return working
                if not self._is_pristine_active_plan_clone(conn, working, active):
                    raise OutlineGateError(
                        "stale outline plan draft contains work and requires explicit recovery"
                    )
                cleared = conn.execute(
                    """
                    UPDATE outline_planning_heads
                    SET working_plan_revision_id = NULL, updated_at = ?
                    WHERE novel_id = ? AND authority_mode = 'manifest'
                      AND active_plan_revision_id = ? AND active_plan_digest = ?
                      AND authority_generation = ? AND projection_generation = ?
                      AND working_plan_revision_id = ?
                    """,
                    (
                        now,
                        novel_id,
                        active.id,
                        active.digest,
                        head.authority_generation,
                        head.projection_generation,
                        working.id,
                    ),
                )
                if cleared.rowcount != 1:
                    raise OutlineGateError(
                        "active outline planning Head changed during stale draft recovery"
                    )
                retired = conn.execute(
                    """
                    UPDATE outline_plan_revisions
                    SET status = 'stale', updated_at = ?
                    WHERE id = ? AND novel_id = ? AND status = 'draft'
                      AND sealed_at IS NULL AND digest = ?
                    """,
                    (now, working.id, novel_id, working.digest),
                )
                if retired.rowcount != 1:
                    raise OutlineGateError(
                        "stale outline plan draft changed during recovery"
                    )
            revision = int(
                conn.execute(
                    "SELECT COALESCE(MAX(revision), 0) + 1 AS revision "
                    "FROM outline_plan_revisions WHERE novel_id = ?",
                    (novel_id,),
                ).fetchone()["revision"]
            )
            plan_id = f"outline-plan-{uuid4()}"
            cloned_items = tuple(
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
            )
            cloned_digest = canonical_plan_digest(
                canonical_prefix_digest=cloned_prefix_digest,
                items=cloned_items,
            )
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
                    cloned_digest,
                    active.digest,
                    replan_start_chapter,
                    cloned_prefix_digest,
                    json.dumps(cloned_boundary, ensure_ascii=False, sort_keys=True),
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
                cloned_items,
                now,
            )
            self._copy_plan_projection_bindings(
                conn,
                source_plan_revision_id=active.id,
                target_plan_revision_id=plan_id,
                expected_item_count=len(active.items),
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

    def _is_pristine_active_plan_clone(
        self,
        conn: sqlite3.Connection,
        working: OutlinePlanRevision,
        active: OutlinePlanRevision,
    ) -> bool:
        """Return whether retiring the draft preserves all user-authored work."""

        if (
            working.novel_id != active.novel_id
            or working.parent_plan_revision_id != active.id
            or working.base_plan_digest != active.digest
            or working.status != PlanRevisionStatus.DRAFT
            or working.sealed_at is not None
            or working.replan_start_chapter is not None
            or working.author_intent
            or working.created_by != "system"
            or working.publish_idempotency_key
            or working.reconciliation_status != active.reconciliation_status
            or dict(working.reconciliation_report or {})
            != dict(active.reconciliation_report or {})
            or len(working.items) != len(active.items)
        ):
            return False

        def item_signature(item: OutlinePlanItem) -> tuple[Any, ...]:
            return (
                item.logical_node_id,
                item.version_id,
                item.version_digest,
                item.level,
                item.sibling_index,
                item.parent_logical_node_id,
                item.expansion_state,
                item.validated_parent_digest,
                item.validated_previous_sibling_digest,
            )

        active_items = {
            item.logical_node_id: item_signature(item) for item in active.items
        }
        if any(
            not item.is_reused
            or active_items.get(item.logical_node_id) != item_signature(item)
            for item in working.items
        ):
            return False
        attempt = conn.execute(
            "SELECT 1 FROM outline_plan_cohort_attempts "
            "WHERE plan_revision_id = ? LIMIT 1",
            (working.id,),
        ).fetchone()
        if attempt is not None:
            return False

        def binding_signature(plan_revision_id: str) -> dict[str, tuple[Any, ...]]:
            return {
                str(row["logical_node_id"]): (
                    row["story_node_id"],
                    row["parent_story_node_id"],
                    row["number"],
                    row["order_index"],
                )
                for row in self.projection_bindings_for_revision(
                    plan_revision_id, _connection=conn
                )
            }

        return binding_signature(working.id) == binding_signature(active.id)

    def replace_draft_cohort_payloads(
        self,
        *,
        plan_revision_id: str,
        parent_logical_node_id: str,
        payloads: Sequence[OutlinePayload],
        expected_parent_digest: Optional[str] = None,
        expected_plan_digest: Optional[str] = None,
        source: OutlineSource = OutlineSource.AI,
    ) -> OutlinePlanRevision:
        """Atomically create and replace one complete direct-child cohort."""

        conn = self._connection()
        if conn.in_transaction:
            raise OutlineGateError("manifest cohort replacement requires a clean connection")
        try:
            conn.execute("BEGIN IMMEDIATE")
            self._replace_draft_cohort_payloads_locked(
                conn,
                plan_revision_id=plan_revision_id,
                parent_logical_node_id=parent_logical_node_id,
                payloads=payloads,
                expected_parent_digest=expected_parent_digest,
                expected_plan_digest=expected_plan_digest,
                source=source,
            )
            conn.commit()
        except BaseException:
            if conn.in_transaction:
                conn.rollback()
            raise
        return self.get_plan_revision(plan_revision_id)

    def _replace_draft_cohort_payloads_locked(
        self,
        conn: sqlite3.Connection,
        *,
        plan_revision_id: str,
        parent_logical_node_id: str,
        payloads: Sequence[OutlinePayload],
        expected_parent_digest: Optional[str] = None,
        expected_plan_digest: Optional[str] = None,
        source: OutlineSource = OutlineSource.AI,
    ) -> OutlinePlanRevision:
        """Replace a draft cohort inside the caller's write transaction."""

        if not conn.in_transaction:
            raise OutlineGateError("manifest cohort replacement requires a write transaction")
        if not payloads:
            raise OutlineGateError("manifest cohort must contain every direct child")
        now = self._now()
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
        if expected_plan_digest and expected_plan_digest != plan.digest:
            raise OutlineGateError("manifest cohort plan digest changed")
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
            item.parent_logical_node_id == parent_logical_node_id for item in plan.items
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
            conn.execute(
                """
                UPDATE outline_contracts
                SET draft_version_id = ?, updated_at = ?
                WHERE id = ? AND novel_id = ?
                """,
                (version_id, now, contract_id, plan.novel_id),
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
                    id=f"outline-plan-item-{uuid4()}",
                )
            )
            previous_digest = payload.digest
        normalized = self._validate_plan_items(
            plan.novel_id, (*plan.items, *children), conn=conn
        )
        digest = canonical_plan_digest(
            canonical_prefix_digest=plan.canonical_prefix_digest,
            items=normalized,
        )
        self._insert_plan_items(conn, plan.id, children, now)
        self._declare_draft_projection_bindings(
            conn,
            plan_revision_id=plan.id,
            parent=parent,
            children=children,
        )
        updated = conn.execute(
            """
            UPDATE outline_plan_revisions
            SET digest = ?, updated_at = ?
            WHERE id = ? AND sealed_at IS NULL AND status = ? AND digest = ?
            """,
            (digest, now, plan.id, plan.status.value, plan.digest),
        )
        if updated.rowcount != 1:
            raise OutlineGateError("manifest draft changed during cohort replacement")
        return self.get_plan_revision(plan_revision_id, _connection=conn)

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

    def _seal_plan_revision_locked(
        self,
        conn: sqlite3.Connection,
        plan_revision_id: str,
        *,
        clear_working_plan: bool,
    ) -> OutlinePlanRevision:
        """Seal a draft inside an already-owned write transaction.

        Cohort publication must keep its draft pointer live until the physical
        projection and Head compare-and-swap both succeed.  The public seal
        API remains a complete operation; this helper is deliberately private
        so no caller can borrow its transaction ownership accidentally.
        """

        if not conn.in_transaction:
            raise OutlineGateError("locked outline plan sealing requires a write transaction")
        now = self._now()
        plan = self.get_plan_revision(plan_revision_id, _connection=conn)
        if plan.sealed_at:
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
            if not clear_working_plan:
                raise OutlineGateError(
                    "duplicate outline plan cannot be sealed before projection activation"
                )
            existing_plan = self.get_plan_revision(
                str(existing["id"]), _connection=conn
            )
            self._validate_plan_projection_bindings(conn, existing_plan)
            cleared = conn.execute(
                """
                UPDATE outline_planning_heads
                SET working_plan_revision_id = NULL, updated_at = ?
                WHERE novel_id = ? AND working_plan_revision_id = ?
                """,
                (now, plan.novel_id, plan.id),
            )
            if cleared.rowcount != 1:
                raise OutlineGateError("outline planning Head changed during duplicate sealing")
            deleted = conn.execute(
                "DELETE FROM outline_plan_revisions WHERE id = ? AND sealed_at IS NULL",
                (plan.id,),
            )
            if deleted.rowcount != 1:
                raise OutlineGateError("outline plan changed during duplicate sealing")
            return existing_plan

        self._snapshot_plan_projection_bindings(conn, plan)
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
        if clear_working_plan:
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
        return self.get_plan_revision(plan.id, _connection=conn)

    def seal_plan_revision(self, plan_revision_id: str) -> OutlinePlanRevision:
        conn = self._connection()
        if conn.in_transaction:
            raise OutlineGateError("outline plan sealing requires a clean connection")
        try:
            # Read every mutable input only after the write lock is held.  A
            # plan digest is meaningful only for this exact locked snapshot.
            conn.execute("BEGIN IMMEDIATE")
            plan = self._seal_plan_revision_locked(
                conn, plan_revision_id, clear_working_plan=True
            )
            conn.commit()
        except BaseException:
            if conn.in_transaction:
                conn.rollback()
            raise
        return plan

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
                self._validate_plan_projection_bindings(conn, plan)
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
            self._snapshot_plan_projection_bindings(
                conn,
                self.get_plan_revision(plan_id, _connection=conn),
            )
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
            scope_values = dict(scope or {})
            parent = next(
                (
                    item
                    for item in plan.items
                    if item.logical_node_id == parent_logical_node_id
                ),
                None,
            )
            if "plan_digest" in scope_values and str(
                scope_values.get("plan_digest") or ""
            ) != plan.digest:
                raise OutlineGateError("manifest cohort scope plan digest changed")
            if (
                "parent_version_digest" in scope_values
                and (
                    parent is None
                    or str(scope_values.get("parent_version_digest") or "")
                    != parent.version_digest
                )
            ):
                raise OutlineGateError("manifest cohort scope parent digest changed")
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
                    json.dumps(scope_values, ensure_ascii=False, sort_keys=True),
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

    def complete_manifest_cohort_attempt_with_payloads(
        self,
        *,
        attempt_id: str,
        payloads: Sequence[OutlinePayload],
        expected_plan_digest: str,
        expected_parent_digest: str,
        expected_context_digest: str,
        context_digest_supplier: Optional[Callable[[sqlite3.Connection], str]] = None,
        source: OutlineSource = OutlineSource.AI,
    ) -> tuple[dict[str, Any], OutlinePlanRevision]:
        """Persist a cohort and complete its running attempt in one transaction."""

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
            try:
                scope = json.loads(row["scope_json"] or "{}")
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise OutlineGateError("manifest cohort attempt scope is invalid") from exc
            if not isinstance(scope, dict):
                raise OutlineGateError("manifest cohort attempt scope is invalid")
            if str(row["context_digest"] or "") != expected_context_digest:
                raise OutlineGateError("manifest cohort context digest changed")
            if str(scope.get("plan_digest") or "") != expected_plan_digest:
                raise OutlineGateError("manifest cohort scope plan digest changed")
            if (
                str(scope.get("parent_version_digest") or "")
                != expected_parent_digest
            ):
                raise OutlineGateError("manifest cohort scope parent digest changed")
            if str(scope.get("parent_logical_node_id") or "") != str(
                row["parent_logical_node_id"]
            ):
                raise OutlineGateError("manifest cohort attempt scope changed")
            plan = self._require_open_manifest_cohort_attempt(
                conn,
                plan_revision_id=str(row["plan_revision_id"]),
                parent_logical_node_id=str(row["parent_logical_node_id"]),
                level=OutlineLevel(str(row["level"])),
            )
            parent = next(
                (
                    item
                    for item in plan.items
                    if item.logical_node_id == row["parent_logical_node_id"]
                ),
                None,
            )
            if plan.digest != expected_plan_digest:
                raise OutlineGateError("manifest cohort plan digest changed")
            if parent is None or parent.version_digest != expected_parent_digest:
                raise OutlineGateError("manifest cohort parent digest changed")
            if context_digest_supplier is not None:
                current_context_digest = context_digest_supplier(conn)
                if current_context_digest != expected_context_digest:
                    raise OutlineGateError("manifest cohort prompt context digest changed")
            updated_plan = self._replace_draft_cohort_payloads_locked(
                conn,
                plan_revision_id=plan.id,
                parent_logical_node_id=parent.logical_node_id,
                payloads=payloads,
                expected_plan_digest=expected_plan_digest,
                expected_parent_digest=expected_parent_digest,
                source=source,
            )
            now = self._now()
            completed = conn.execute(
                """
                UPDATE outline_plan_cohort_attempts
                SET status = 'completed', error = '', completed_at = ?, updated_at = ?
                WHERE id = ? AND status = 'running'
                """,
                (now, now, attempt_id),
            )
            if completed.rowcount != 1:
                raise OutlineGateError("manifest cohort attempt changed during completion")
            self._append_manifest_cohort_attempt_event(
                conn, attempt_id, {"type": "completed"}
            )
            conn.commit()
        except BaseException:
            if conn.in_transaction:
                conn.rollback()
            raise
        return self.get_manifest_cohort_attempt(attempt_id), updated_plan

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

    def _manifest_cohort_attempt_completion_error(
        self,
        conn: sqlite3.Connection,
        row: sqlite3.Row,
    ) -> Optional[str]:
        """Return a blocker when a restarted attempt has no complete Working Plan."""

        plan_revision_id = str(row["plan_revision_id"])
        parent_logical_node_id = str(row["parent_logical_node_id"])
        level = OutlineLevel(str(row["level"]))
        plan = self.get_plan_revision(plan_revision_id, _connection=conn)
        head = self.get_planning_head(plan.novel_id)
        if head.authority_mode != PlanningAuthorityMode.MANIFEST:
            return "manifest planning authority is unavailable"
        if head.working_plan_revision_id != plan.id:
            return "Working Plan is no longer the manifest working revision"
        if plan.sealed_at or plan.status not in {
            PlanRevisionStatus.DRAFT,
            PlanRevisionStatus.GENERATING,
            PlanRevisionStatus.VALIDATING,
        }:
            return "Working Plan is no longer editable"

        scope = json.loads(row["scope_json"] or "{}")
        if not isinstance(scope, dict):
            return "cohort attempt scope is invalid"
        if str(scope.get("parent_logical_node_id") or "") != parent_logical_node_id:
            return "cohort attempt parent scope changed"

        parent = next(
            (item for item in plan.items if item.logical_node_id == parent_logical_node_id),
            None,
        )
        if parent is None:
            return "cohort attempt parent is missing from Working Plan"
        if parent.level.child_level != level:
            return "cohort attempt level no longer belongs to its parent"
        if str(scope.get("parent_version_digest") or "") != parent.version_digest:
            return "Working Plan parent digest does not match attempt scope"

        children = tuple(
            item
            for item in plan.items
            if item.parent_logical_node_id == parent_logical_node_id
            and item.level == level
        )
        if not children:
            return "Working Plan cohort children are incomplete"

        # The cohort write itself changes the Working Plan digest.  The
        # attempt scope pins the pre-call digest; once direct children exist,
        # the new digest is expected and the structural validators below are
        # the authority for deciding whether the write is complete.

        normalized = self._validate_plan_items(plan.novel_id, plan.items, conn=conn)
        self._validate_plan_cohorts(conn, normalized)
        self._validate_plan_projection_bindings(conn, plan, allow_unmaterialized=True)
        return None

    def recover_manifest_cohort_attempt_after_service_restart(
        self,
        attempt_id: str,
        *,
        reason: str = "service_restart_interrupted",
    ) -> dict[str, Any]:
        """Close one orphaned Cohort attempt without creating outline children.

        A process may die after the Working Plan transaction commits but before
        the attempt row is marked complete.  Recovery treats the durable plan as
        authoritative and only changes the attempt status.
        """

        conn = self._connection()
        if conn.in_transaction:
            raise OutlineGateError("manifest cohort recovery requires a clean connection")
        try:
            conn.execute("BEGIN IMMEDIATE")
            row = conn.execute(
                "SELECT * FROM outline_plan_cohort_attempts WHERE id = ?", (attempt_id,)
            ).fetchone()
            if row is None:
                raise KeyError(f"outline cohort attempt not found: {attempt_id}")
            old_status = str(row["status"])
            if old_status != "running":
                conn.commit()
                return self.get_manifest_cohort_attempt(attempt_id)

            try:
                blocker = self._manifest_cohort_attempt_completion_error(conn, row)
            except Exception as exc:
                blocker = str(exc)
            new_status = "completed" if blocker is None else "failed"
            error = "" if blocker is None else reason
            now = self._now()
            updated = conn.execute(
                """
                UPDATE outline_plan_cohort_attempts
                SET status = ?, error = ?, completed_at = ?, updated_at = ?
                WHERE id = ? AND status = 'running'
                """,
                (new_status, error, now, now, attempt_id),
            )
            if updated.rowcount != 1:
                raise OutlineGateError("manifest cohort attempt changed during recovery")
            event = {
                "type": "recovered",
                "reason": reason,
                "old_status": old_status,
                "new_status": new_status,
            }
            if blocker:
                event["blocker"] = blocker
            self._append_manifest_cohort_attempt_event(conn, attempt_id, event)
            conn.commit()
        except BaseException:
            if conn.in_transaction:
                conn.rollback()
            raise
        return self.get_manifest_cohort_attempt(attempt_id)

    def recover_manifest_cohort_attempts_after_service_restart(
        self,
        *,
        reason: str = "service_restart_interrupted",
    ) -> list[dict[str, Any]]:
        """Recover every running Cohort attempt exactly once, per novel."""

        conn = self._connection()
        rows = conn.execute(
            """
            SELECT attempt.id, attempt.plan_revision_id,
                   attempt.parent_logical_node_id, attempt.level,
                   revision.novel_id
            FROM outline_plan_cohort_attempts AS attempt
            JOIN outline_plan_revisions AS revision
              ON revision.id = attempt.plan_revision_id
            WHERE attempt.status = 'running'
            ORDER BY revision.novel_id, attempt.created_at, attempt.id
            """
        ).fetchall()
        recovered: list[dict[str, Any]] = []
        by_novel: dict[str, list[sqlite3.Row]] = {}
        for row in rows:
            by_novel.setdefault(str(row["novel_id"]), []).append(row)
        for novel_id, novel_rows in by_novel.items():
            try:
                for row in novel_rows:
                    attempt = self.recover_manifest_cohort_attempt_after_service_restart(
                        str(row["id"]), reason=reason
                    )
                    result = {
                        "attempt_id": str(attempt["id"]),
                        "novel_id": novel_id,
                        "plan_revision_id": str(attempt["plan_revision_id"]),
                        "parent_logical_node_id": str(attempt["parent_logical_node_id"]),
                        "level": str(attempt["level"]),
                        "old_status": "running",
                        "new_status": str(attempt["status"]),
                        "reason": reason,
                        "attempt": attempt,
                    }
                    recovered.append(result)
                    logger.info(
                        "Startup: recovered cohort attempt=%s novel=%s plan=%s parent=%s "
                        "level=%s old_status=%s new_status=%s reason=%s",
                        result["attempt_id"],
                        result["novel_id"],
                        result["plan_revision_id"],
                        result["parent_logical_node_id"],
                        result["level"],
                        result["old_status"],
                        result["new_status"],
                        result["reason"],
                    )
            except Exception:
                logger.exception(
                    "Startup: cohort recovery isolated failure novel=%s reason=%s",
                    novel_id,
                    reason,
                )
        return recovered

    # Short alias used by startup orchestration and compatible with the other
    # persistence recovery entry points in this repository.
    def recover_all_manifest_cohort_attempts_after_service_restart(
        self,
        *,
        reason: str = "service_restart_interrupted",
    ) -> list[dict[str, Any]]:
        return self.recover_manifest_cohort_attempts_after_service_restart(reason=reason)

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
