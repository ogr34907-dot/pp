"""Atomic Manifest Head and physical StoryNode projection writer."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
import sqlite3
from collections.abc import Sequence
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any, Iterator, Optional

from domain.structure.outline_contract import OutlineLevel, OutlinePayload
from domain.structure.outline_plan import OutlinePlanItem, canonical_plan_digest
from domain.structure.story_node import (
    NodeType,
    PlanningSource,
    PlanningStatus,
    StoryNode,
)

from infrastructure.persistence.database.planning_authority_guard import (
    PlanningAuthorityError,
    ProjectionWriteCapability,
    _begin_projection_writer_session,
    _end_projection_writer_session,
    _mark_projection_activated,
    _mint_projection_capability,
    _validate_projection_capability,
)

if TYPE_CHECKING:
    from infrastructure.persistence.database.story_node_repository import StoryNodeRepository


_LEVEL_DEPTH = {
    "outline": 0,
    "part": 1,
    "volume": 2,
    "act": 3,
    "chapter": 4,
}


class _JoinedProjectionSession:
    """Scope a capability to the caller's live transaction without SQL nesting."""

    __slots__ = ("connection", "connection_identity", "active")

    def __init__(self, conn: sqlite3.Connection) -> None:
        if not conn.in_transaction:
            raise PlanningAuthorityError(
                "bound projection requires a live caller transaction"
            )
        self.connection = conn
        self.connection_identity = id(conn)
        self.active = True

    def heartbeat(self, conn: sqlite3.Connection) -> None:
        if not self.active or conn is not self.connection or not conn.in_transaction:
            self.active = False
            raise PlanningAuthorityError(
                "StoryNode projection transaction has ended"
            )

    def close(self, conn: sqlite3.Connection) -> None:
        del conn
        self.active = False


@dataclass(frozen=True)
class _BoundProjectionItem:
    item_id: str
    logical_node_id: str
    version_id: str
    version_digest: str
    version_source: str
    level: str
    parent_logical_node_id: Optional[str]
    sibling_index: int
    expansion_state: str
    validated_parent_digest: str
    validated_previous_sibling_digest: str
    is_reused: bool
    story_node_id: Optional[str]
    parent_story_node_id: Optional[str]
    number: Optional[int]
    order_index: Optional[int]
    payload: OutlinePayload


class PlanProjectionWriter:
    """Keep projection DML and the book-level Head in one SQLite transaction."""

    def __init__(self, repository: "StoryNodeRepository") -> None:
        self._repository = repository

    @contextmanager
    def _projection_transaction(
        self, *, novel_id: str, plan_revision_id: str, operation: str,
        expected_active_plan_revision_id: Optional[str], expected_active_plan_digest: str,
        expected_authority_generation: int, expected_projection_generation: int,
    ) -> Iterator[ProjectionWriteCapability]:
        conn = self._repository._get_connection()
        session = _begin_projection_writer_session(conn)
        capability: Optional[ProjectionWriteCapability] = None
        try:
            capability = _mint_projection_capability(
                conn, novel_id=novel_id, plan_revision_id=plan_revision_id,
                designated_operation=operation, authority_generation=expected_authority_generation,
                projection_generation=expected_projection_generation,
                expected_active_plan_revision_id=expected_active_plan_revision_id,
                expected_active_plan_digest=expected_active_plan_digest, _session=session,
            )
            yield capability
            if capability is None or not capability.activated:
                raise PlanningAuthorityError("projection transaction must activate its designated Head")
            _validate_projection_capability(conn, novel_id, capability)
            conn.commit()
        except BaseException:
            if conn.in_transaction:
                conn.rollback()
            raise
        finally:
            if capability is not None:
                capability._expire()
            _end_projection_writer_session(conn, session)

    def _activate_head(self, capability: ProjectionWriteCapability) -> None:
        conn = self._repository._get_connection()
        _validate_projection_capability(conn, capability.novel_id, capability)
        if capability.activated:
            raise PlanningAuthorityError("projection Head has already been activated")
        target = conn.execute(
            "SELECT digest, sealed_at, status, reconciliation_status FROM outline_plan_revisions "
            "WHERE id = ? AND novel_id = ?", (capability.plan_revision_id, capability.novel_id)
        ).fetchone()
        if (target is None or not target["sealed_at"]
            or str(target["status"] or "") != "ready_for_review"
            or str(target["reconciliation_status"] or "") != "aligned"
            or str(target["digest"] or "") != capability.target_digest):
            raise PlanningAuthorityError("projection target must remain sealed, aligned, and ready_for_review")
        old_generation = capability._expected_authority_generation
        old_projection = capability._expected_projection_generation
        new_generation = old_generation + 1
        try:
            updated = conn.execute(
                """UPDATE outline_planning_heads
                   SET authority_mode = 'manifest', authority_generation = ?,
                       active_plan_revision_id = ?, active_plan_digest = ?,
                       working_plan_revision_id = NULL, projection_generation = ?,
                       updated_at = CURRENT_TIMESTAMP
                 WHERE novel_id = ? AND authority_mode = ?
                   AND authority_generation = ? AND projection_generation = ?
                   AND active_plan_revision_id IS ? AND active_plan_digest = ?""",
                (new_generation, capability.plan_revision_id, capability.target_digest,
                 new_generation, capability.novel_id,
                 "legacy" if capability.designated_operation == "cutover" else "manifest",
                 old_generation, old_projection, capability._expected_active_plan_revision_id,
                 capability._expected_active_plan_digest),
            )
        except sqlite3.Error as exc:
            raise PlanningAuthorityError(str(exc)) from exc
        if updated.rowcount != 1:
            raise PlanningAuthorityError("planning Head changed before activation")
        _mark_projection_activated(conn, capability)

    @staticmethod
    def _bound_items(
        conn: sqlite3.Connection,
        *,
        novel_id: str,
        plan_revision_id: str,
    ) -> tuple[_BoundProjectionItem, ...]:
        revision = conn.execute(
            "SELECT novel_id, digest, canonical_prefix_digest, sealed_at "
            "FROM outline_plan_revisions WHERE id = ?",
            (plan_revision_id,),
        ).fetchone()
        if (
            revision is None
            or str(revision["novel_id"] or "") != novel_id
            or not revision["sealed_at"]
            or not str(revision["digest"] or "")
        ):
            raise PlanningAuthorityError(
                "projection plan must be a matching sealed revision"
            )
        expected_count = int(
            conn.execute(
                "SELECT COUNT(*) FROM outline_plan_revision_items "
                "WHERE plan_revision_id = ?",
                (plan_revision_id,),
            ).fetchone()[0]
        )
        rows = conn.execute(
            """
            SELECT item.id AS item_id, item.logical_node_id, item.version_id,
                   item.parent_logical_node_id, item.level,
                   item.sibling_index, item.expansion_state,
                   item.validated_parent_digest,
                   item.validated_previous_sibling_digest, item.is_reused,
                   contract.novel_id AS contract_novel_id,
                   contract.level AS contract_level,
                   version.contract_id AS version_contract_id,
                   version.digest AS version_digest,
                   version.source AS version_source,
                   version.payload_json, version.sealed_at AS version_sealed_at,
                   binding.plan_revision_item_id AS binding_item_id,
                   binding.story_node_id, binding.parent_story_node_id,
                   binding.number, binding.order_index
            FROM outline_plan_revision_items AS item
            LEFT JOIN outline_contracts AS contract
              ON contract.id = item.logical_node_id
            LEFT JOIN outline_contract_versions AS version
              ON version.id = item.version_id
            LEFT JOIN outline_plan_projection_bindings AS binding
              ON binding.plan_revision_item_id = item.id
            WHERE item.plan_revision_id = ?
            ORDER BY CASE item.level
                WHEN 'outline' THEN 0 WHEN 'part' THEN 1 WHEN 'volume' THEN 2
                WHEN 'act' THEN 3 ELSE 4 END,
                COALESCE(item.parent_logical_node_id, ''), item.sibling_index,
                item.logical_node_id
            """,
            (plan_revision_id,),
        ).fetchall()
        if expected_count < 1 or len(rows) != expected_count:
            raise PlanningAuthorityError("sealed projection item set is incomplete")

        items: list[_BoundProjectionItem] = []
        logical_ids: set[str] = set()
        physical_ids: set[str] = set()
        for row in rows:
            item_id = str(row["item_id"] or "")
            logical_node_id = str(row["logical_node_id"] or "")
            version_id = str(row["version_id"] or "")
            level = str(row["level"] or "")
            if (
                not item_id
                or not logical_node_id
                or logical_node_id in logical_ids
                or level not in _LEVEL_DEPTH
                or row["binding_item_id"] is None
                or str(row["contract_novel_id"] or "") != novel_id
                or str(row["contract_level"] or "") != level
                or str(row["version_contract_id"] or "") != logical_node_id
                or not row["version_sealed_at"]
            ):
                raise PlanningAuthorityError(
                    "sealed projection binding or version set is incomplete"
                )
            logical_ids.add(logical_node_id)
            try:
                payload = OutlinePayload.from_dict(
                    json.loads(str(row["payload_json"] or "{}"))
                )
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise PlanningAuthorityError(
                    "sealed projection version payload is invalid"
                ) from exc
            version_digest = str(row["version_digest"] or "")
            if not version_digest or payload.digest != version_digest:
                raise PlanningAuthorityError(
                    "sealed projection version digest does not match its payload"
                )

            binding_values = (
                row["story_node_id"],
                row["parent_story_node_id"],
                row["number"],
                row["order_index"],
            )
            story_node_id = (
                str(row["story_node_id"]) if row["story_node_id"] is not None else None
            )
            if level == "outline":
                if any(value is not None for value in binding_values):
                    raise PlanningAuthorityError(
                        "outline root projection binding must be unbound"
                    )
            elif story_node_id is None:
                if any(value is not None for value in binding_values) or str(
                    row["expansion_state"] or ""
                ) != "unexpanded":
                    raise PlanningAuthorityError(
                        "expanded projection item has no physical binding"
                    )
            else:
                if row["number"] is None or row["order_index"] is None:
                    raise PlanningAuthorityError(
                        "physical projection binding is incomplete"
                    )
                if story_node_id in physical_ids:
                    raise PlanningAuthorityError(
                        "sealed projection has duplicate physical bindings"
                    )
                physical_ids.add(story_node_id)

            items.append(
                _BoundProjectionItem(
                    item_id=item_id,
                    logical_node_id=logical_node_id,
                    version_id=version_id,
                    version_digest=version_digest,
                    version_source=str(row["version_source"] or "ai"),
                    level=level,
                    parent_logical_node_id=(
                        str(row["parent_logical_node_id"])
                        if row["parent_logical_node_id"] is not None
                        else None
                    ),
                    sibling_index=int(row["sibling_index"]),
                    expansion_state=str(row["expansion_state"] or "unexpanded"),
                    validated_parent_digest=str(
                        row["validated_parent_digest"] or ""
                    ),
                    validated_previous_sibling_digest=str(
                        row["validated_previous_sibling_digest"] or ""
                    ),
                    is_reused=bool(row["is_reused"]),
                    story_node_id=story_node_id,
                    parent_story_node_id=(
                        str(row["parent_story_node_id"])
                        if row["parent_story_node_id"] is not None
                        else None
                    ),
                    number=int(row["number"]) if row["number"] is not None else None,
                    order_index=(
                        int(row["order_index"])
                        if row["order_index"] is not None
                        else None
                    ),
                    payload=payload,
                )
            )

        roots = [
            item
            for item in items
            if item.level == "outline" and item.parent_logical_node_id is None
        ]
        if len(roots) != 1:
            raise PlanningAuthorityError(
                "sealed projection requires exactly one unbound outline root"
            )
        by_logical_id = {item.logical_node_id: item for item in items}
        sibling_groups: dict[tuple[str, str], list[_BoundProjectionItem]] = {}
        for item in items:
            if item.level == "outline":
                if (
                    item.parent_logical_node_id is not None
                    or item.validated_parent_digest
                ):
                    raise PlanningAuthorityError(
                        "outline root projection binding has a logical parent"
                    )
                continue
            if not item.parent_logical_node_id:
                raise PlanningAuthorityError(
                    "physical projection binding has no logical parent"
                )
            parent = by_logical_id.get(item.parent_logical_node_id)
            if parent is None or _LEVEL_DEPTH[item.level] != _LEVEL_DEPTH[parent.level] + 1:
                raise PlanningAuthorityError(
                    "physical projection binding parent is invalid"
                )
            if item.validated_parent_digest != parent.version_digest:
                raise PlanningAuthorityError(
                    "projection item parent digest does not match"
                )
            if item.story_node_id is not None:
                if item.parent_story_node_id != parent.story_node_id:
                    raise PlanningAuthorityError(
                        "physical projection binding parent does not match"
                    )
                if item.level != "part" and parent.story_node_id is None:
                    raise PlanningAuthorityError(
                        "physical projection binding has an unbound parent"
                    )
            sibling_groups.setdefault(
                (item.parent_logical_node_id, item.level), []
            ).append(item)

        for siblings in sibling_groups.values():
            siblings.sort(key=lambda item: item.sibling_index)
            for index, item in enumerate(siblings):
                if item.sibling_index != index:
                    raise PlanningAuthorityError(
                        "sealed projection sibling indexes are not contiguous"
                    )
                expected_previous = "" if index == 0 else siblings[index - 1].version_digest
                if item.validated_previous_sibling_digest != expected_previous:
                    raise PlanningAuthorityError(
                        "projection sibling handoff digest does not match"
                    )
        recomputed_digest = canonical_plan_digest(
            canonical_prefix_digest=str(revision["canonical_prefix_digest"] or ""),
            items=(
                OutlinePlanItem(
                    id=item.item_id,
                    logical_node_id=item.logical_node_id,
                    version_id=item.version_id,
                    version_digest=item.version_digest,
                    level=OutlineLevel(item.level),
                    sibling_index=item.sibling_index,
                    parent_logical_node_id=item.parent_logical_node_id,
                    expansion_state=item.expansion_state,
                    validated_parent_digest=item.validated_parent_digest,
                    validated_previous_sibling_digest=(
                        item.validated_previous_sibling_digest
                    ),
                    is_reused=item.is_reused,
                )
                for item in items
            ),
        )
        if recomputed_digest != str(revision["digest"] or ""):
            raise PlanningAuthorityError(
                "sealed projection plan digest does not match its items"
            )
        return tuple(items)

    @staticmethod
    def _existing_node(
        conn: sqlite3.Connection, story_node_id: str
    ) -> Optional[sqlite3.Row]:
        return conn.execute(
            "SELECT * FROM story_nodes WHERE id = ?", (story_node_id,)
        ).fetchone()

    def _materialized_node(
        self,
        item: _BoundProjectionItem,
        *,
        novel_id: str,
        existing_row: Optional[sqlite3.Row],
        plan_revision_id: str,
    ) -> StoryNode:
        if item.story_node_id is None or item.number is None or item.order_index is None:
            raise PlanningAuthorityError("cannot materialize an unbound outline item")
        from infrastructure.persistence.database.story_node_repository import (
            StoryNodeRepository,
        )

        existing = (
            StoryNodeRepository._row_to_entity(self._repository, existing_row)
            if existing_row is not None
            else None
        )
        payload = item.payload
        metadata: dict[str, Any] = dict(existing.metadata) if existing else {}
        metadata.update(
            {
                "manifest.logical_node_id": item.logical_node_id,
                "manifest.plan_revision_id": plan_revision_id,
                "manifest.plan_revision_item_id": item.item_id,
                "manifest.version_id": item.version_id,
                "manifest.version_digest": item.version_digest,
            }
        )
        range_count = (
            payload.chapter_end - payload.chapter_start + 1
            if payload.chapter_start is not None
            and payload.chapter_end is not None
            and payload.chapter_end >= payload.chapter_start
            else None
        )
        source = str(item.version_source or "ai")
        planning_status = (
            PlanningStatus.USER_EDITED
            if source == "author"
            else PlanningStatus.AI_GENERATED
        )
        planning_source = (
            PlanningSource.MANUAL
            if source in {"author", "imported"}
            else (
                PlanningSource.AI_ACT
                if item.level in {"act", "chapter"}
                else PlanningSource.AI_MACRO
            )
        )
        extra_themes = payload.extra.get("themes")
        themes = (
            [str(value) for value in extra_themes]
            if isinstance(extra_themes, list)
            else (list(existing.themes) if existing else [])
        )
        now = datetime.now()
        return StoryNode(
            id=item.story_node_id,
            novel_id=novel_id,
            parent_id=item.parent_story_node_id,
            node_type=NodeType(item.level),
            number=item.number,
            title=payload.title,
            description=payload.narrative_text,
            order_index=item.order_index,
            planning_status=planning_status,
            planning_source=planning_source,
            chapter_start=payload.chapter_start,
            chapter_end=payload.chapter_end,
            chapter_count=(
                range_count
                if range_count is not None
                else (existing.chapter_count if existing else 0)
            ),
            suggested_chapter_count=(
                range_count
                if range_count is not None
                else (existing.suggested_chapter_count if existing else None)
            ),
            content=existing.content if existing else None,
            outline=payload.narrative_text,
            word_count=existing.word_count if existing else 0,
            status=existing.status if existing else "draft",
            themes=themes,
            key_events=list(payload.required_events),
            narrative_arc=payload.creative_goal or None,
            conflicts=list(payload.conflicts),
            pov_character_id=payload.pov or (existing.pov_character_id if existing else None),
            timeline_start=existing.timeline_start if existing else None,
            timeline_end=existing.timeline_end if existing else None,
            metadata=metadata,
            created_at=existing.created_at if existing else now,
            updated_at=now,
        )

    @staticmethod
    def _delete_materialized_node(
        conn: sqlite3.Connection,
        capability: ProjectionWriteCapability,
        *,
        novel_id: str,
        story_node_id: str,
    ) -> bool:
        _validate_projection_capability(conn, novel_id, capability)
        deleted = conn.execute(
            "DELETE FROM story_nodes WHERE id = ? AND novel_id = ?",
            (story_node_id, novel_id),
        )
        return deleted.rowcount == 1

    @staticmethod
    def _save_materialized_nodes(
        conn: sqlite3.Connection,
        capability: ProjectionWriteCapability,
        *,
        novel_id: str,
        nodes: Sequence[StoryNode],
    ) -> None:
        _validate_projection_capability(conn, novel_id, capability)
        for node in nodes:
            if node.novel_id != novel_id:
                raise PlanningAuthorityError(
                    "bound projection contains a cross-novel StoryNode"
                )
            conn.execute(
                """
                INSERT INTO story_nodes (
                    id, novel_id, parent_id, node_type, number, title, description,
                    order_index, planning_status, planning_source, chapter_start,
                    chapter_end, chapter_count, suggested_chapter_count, content,
                    outline, word_count, status, themes, key_events, narrative_arc,
                    conflicts, pov_character_id, timeline_start, timeline_end,
                    metadata, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                          ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(id) DO UPDATE SET
                    novel_id = excluded.novel_id,
                    parent_id = excluded.parent_id,
                    node_type = excluded.node_type,
                    number = excluded.number,
                    title = excluded.title,
                    description = excluded.description,
                    order_index = excluded.order_index,
                    planning_status = excluded.planning_status,
                    planning_source = excluded.planning_source,
                    chapter_start = excluded.chapter_start,
                    chapter_end = excluded.chapter_end,
                    chapter_count = excluded.chapter_count,
                    suggested_chapter_count = excluded.suggested_chapter_count,
                    content = excluded.content,
                    outline = excluded.outline,
                    word_count = excluded.word_count,
                    status = excluded.status,
                    themes = excluded.themes,
                    key_events = excluded.key_events,
                    narrative_arc = excluded.narrative_arc,
                    conflicts = excluded.conflicts,
                    pov_character_id = excluded.pov_character_id,
                    timeline_start = excluded.timeline_start,
                    timeline_end = excluded.timeline_end,
                    metadata = excluded.metadata,
                    updated_at = excluded.updated_at
                """,
                (
                    node.id,
                    node.novel_id,
                    node.parent_id,
                    node.node_type.value,
                    node.number,
                    node.title,
                    node.description,
                    node.order_index,
                    node.planning_status.value,
                    node.planning_source.value,
                    node.chapter_start,
                    node.chapter_end,
                    node.chapter_count,
                    node.suggested_chapter_count,
                    node.content,
                    node.outline,
                    node.word_count,
                    node.status,
                    json.dumps(node.themes),
                    json.dumps(node.key_events),
                    node.narrative_arc,
                    json.dumps(node.conflicts),
                    node.pov_character_id,
                    node.timeline_start,
                    node.timeline_end,
                    json.dumps(node.metadata),
                    node.created_at.isoformat(),
                    node.updated_at.isoformat(),
                ),
            )

    @staticmethod
    def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
        try:
            return conn.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                (table,),
            ).fetchone() is not None
        except sqlite3.DatabaseError as exc:
            raise PlanningAuthorityError(
                "cannot verify StoryNode projection references"
            ) from exc

    @classmethod
    def _assert_materialized_projection(
        cls,
        conn: sqlite3.Connection,
        *,
        novel_id: str,
        plan_revision_id: str,
        items: tuple[_BoundProjectionItem, ...],
    ) -> None:
        expected = {
            item.story_node_id: item
            for item in items
            if item.story_node_id is not None
        }
        rows = conn.execute(
            "SELECT * FROM story_nodes WHERE novel_id = ?", (novel_id,)
        ).fetchall()
        actual = {str(row["id"]): row for row in rows}
        extra = set(actual) - set(expected)
        if extra:
            raise PlanningAuthorityError(
                "materialized projection contains an extra StoryNode"
            )
        if set(actual) != set(expected):
            raise PlanningAuthorityError(
                "materialized projection is missing a bound StoryNode"
            )
        for story_node_id, item in expected.items():
            row = actual[story_node_id]
            if (
                str(row["node_type"] or "") != item.level
                or row["parent_id"] != item.parent_story_node_id
                or int(row["number"]) != item.number
                or int(row["order_index"]) != item.order_index
            ):
                raise PlanningAuthorityError(
                    "materialized StoryNode does not match its binding"
                )
            try:
                metadata = json.loads(str(row["metadata"] or "{}"))
            except (TypeError, ValueError, json.JSONDecodeError) as exc:
                raise PlanningAuthorityError(
                    "materialized StoryNode provenance is invalid"
                ) from exc
            expected_provenance = {
                "manifest.logical_node_id": item.logical_node_id,
                "manifest.plan_revision_id": plan_revision_id,
                "manifest.plan_revision_item_id": item.item_id,
                "manifest.version_id": item.version_id,
                "manifest.version_digest": item.version_digest,
            }
            if not isinstance(metadata, dict) or any(
                metadata.get(key) != value
                for key, value in expected_provenance.items()
            ):
                raise PlanningAuthorityError(
                    "materialized StoryNode version provenance does not match"
                )

    @classmethod
    def _assert_deletes_are_unreferenced(
        cls,
        conn: sqlite3.Connection,
        *,
        novel_id: str,
        delete_ids: set[str],
    ) -> None:
        if not delete_ids:
            return
        placeholders = ", ".join("?" for _ in delete_ids)
        params = tuple(sorted(delete_ids))
        dangling_child = conn.execute(
            f"SELECT id FROM story_nodes WHERE parent_id IN ({placeholders}) "
            f"AND id NOT IN ({placeholders}) LIMIT 1",
            (*params, *params),
        ).fetchone()
        if dangling_child is not None:
            raise PlanningAuthorityError(
                "projection would delete a StoryNode referenced by a retained child"
            )
        for table, column in (
            ("chapter_elements", "chapter_id"),
            ("chapter_scenes", "chapter_id"),
            ("triple_provenance", "story_node_id"),
        ):
            if not cls._table_exists(conn, table):
                continue
            try:
                referenced = conn.execute(
                    f"SELECT 1 FROM {table} WHERE {column} IN ({placeholders}) LIMIT 1",
                    params,
                ).fetchone()
            except sqlite3.DatabaseError as exc:
                raise PlanningAuthorityError(
                    "cannot verify StoryNode projection references"
                ) from exc
            if referenced is not None:
                raise PlanningAuthorityError(
                    "projection would delete a referenced StoryNode"
                )

        chapter_rows = conn.execute(
            f"SELECT number FROM story_nodes WHERE id IN ({placeholders}) "
            "AND novel_id = ? AND node_type = 'chapter'",
            (*params, novel_id),
        ).fetchall()
        chapter_numbers = sorted({int(row[0]) for row in chapter_rows})
        if not chapter_numbers:
            return
        number_placeholders = ", ".join("?" for _ in chapter_numbers)
        formal_params = (novel_id, *chapter_numbers)
        for table in (
            "pre_candidate_formal_history",
            "chapter_candidate_formal_commits",
            "chapter_narrative_commits",
        ):
            if not cls._table_exists(conn, table):
                continue
            try:
                formal = conn.execute(
                    f"SELECT 1 FROM {table} WHERE novel_id = ? "
                    f"AND chapter_number IN ({number_placeholders}) LIMIT 1",
                    formal_params,
                ).fetchone()
            except sqlite3.DatabaseError as exc:
                raise PlanningAuthorityError(
                    "cannot verify Formal StoryNode identity"
                ) from exc
            if formal is not None:
                raise PlanningAuthorityError(
                    "projection cannot delete a Formal StoryNode"
                )
        if cls._table_exists(conn, "chapters"):
            authored = conn.execute(
                "SELECT 1 FROM chapters WHERE novel_id = ? "
                f"AND number IN ({number_placeholders}) "
                "AND TRIM(COALESCE(content, '')) <> '' LIMIT 1",
                formal_params,
            ).fetchone()
            if authored is not None:
                raise PlanningAuthorityError(
                    "projection cannot delete an authored StoryNode"
                )
        if cls._table_exists(conn, "chapter_candidates"):
            candidate = conn.execute(
                "SELECT 1 FROM chapter_candidates WHERE novel_id = ? "
                f"AND chapter_number IN ({number_placeholders}) "
                "AND status IN ('streaming', 'auditing', 'awaiting_review', "
                "'committing', 'syncing', 'regenerating') LIMIT 1",
                formal_params,
            ).fetchone()
            if candidate is not None:
                raise PlanningAuthorityError(
                    "projection cannot delete a Candidate-referenced StoryNode"
                )

    @staticmethod
    def _activate_bound_head(
        conn: sqlite3.Connection,
        capability: ProjectionWriteCapability,
        *,
        expected_working_plan_revision_id: Optional[str],
    ) -> None:
        _validate_projection_capability(conn, capability.novel_id, capability)
        if capability.activated:
            raise PlanningAuthorityError("projection Head has already been activated")
        target = conn.execute(
            "SELECT digest, sealed_at, status, reconciliation_status "
            "FROM outline_plan_revisions WHERE id = ? AND novel_id = ?",
            (capability.plan_revision_id, capability.novel_id),
        ).fetchone()
        if (
            target is None
            or not target["sealed_at"]
            or str(target["status"] or "") != "ready_for_review"
            or str(target["reconciliation_status"] or "") != "aligned"
            or str(target["digest"] or "") != capability.target_digest
        ):
            raise PlanningAuthorityError(
                "projection target must remain sealed, aligned, and ready_for_review"
            )
        old_authority = capability._expected_authority_generation
        old_projection = capability._expected_projection_generation
        if old_authority != old_projection:
            raise PlanningAuthorityError(
                "manifest authority and projection generations diverged"
            )
        try:
            updated = conn.execute(
                """
                UPDATE outline_planning_heads
                SET authority_generation = ?, active_plan_revision_id = ?,
                    active_plan_digest = ?, working_plan_revision_id = NULL,
                    projection_generation = ?, updated_at = CURRENT_TIMESTAMP
                WHERE novel_id = ? AND authority_mode = 'manifest'
                  AND authority_generation = ? AND projection_generation = ?
                  AND active_plan_revision_id IS ? AND active_plan_digest = ?
                  AND working_plan_revision_id IS ?
                """,
                (
                    old_authority + 1,
                    capability.plan_revision_id,
                    capability.target_digest,
                    old_projection + 1,
                    capability.novel_id,
                    old_authority,
                    old_projection,
                    capability._expected_active_plan_revision_id,
                    capability._expected_active_plan_digest,
                    expected_working_plan_revision_id,
                ),
            )
        except sqlite3.Error as exc:
            raise PlanningAuthorityError(str(exc)) from exc
        if updated.rowcount != 1:
            raise PlanningAuthorityError("planning Head changed before activation")
        _mark_projection_activated(conn, capability)

    async def apply_bound_projection(
        self,
        conn: sqlite3.Connection,
        *,
        novel_id: str,
        plan_revision_id: str,
        expected_active_plan_revision_id: Optional[str],
        expected_active_plan_digest: str,
        expected_authority_generation: int,
        expected_projection_generation: int,
        expected_working_plan_revision_id: Optional[str],
        operation: str = "publish",
    ) -> None:
        """Materialize one sealed binding set inside the caller's transaction."""

        if conn is not self._repository._get_connection():
            raise PlanningAuthorityError(
                "bound projection must use the repository connection"
            )
        if not conn.in_transaction:
            raise PlanningAuthorityError(
                "bound projection requires an open BEGIN IMMEDIATE transaction"
            )
        if operation not in {"publish", "restore"}:
            raise PlanningAuthorityError(
                "bound projection operation must be publish or restore"
            )
        if operation == "publish" and expected_working_plan_revision_id != plan_revision_id:
            raise PlanningAuthorityError(
                "bound projection target must be the expected working plan"
            )
        if operation == "restore" and expected_working_plan_revision_id is not None:
            raise PlanningAuthorityError(
                "bound projection restore requires no working plan draft"
            )

        session: Optional[_JoinedProjectionSession] = None
        capability: Optional[ProjectionWriteCapability] = None
        try:
            session = _JoinedProjectionSession(conn)
            capability = _mint_projection_capability(
                conn,
                novel_id=novel_id,
                plan_revision_id=plan_revision_id,
                designated_operation=operation,
                authority_generation=expected_authority_generation,
                projection_generation=expected_projection_generation,
                expected_active_plan_revision_id=expected_active_plan_revision_id,
                expected_active_plan_digest=expected_active_plan_digest,
                _session=session,
            )
            working = conn.execute(
                "SELECT working_plan_revision_id FROM outline_planning_heads "
                "WHERE novel_id = ?",
                (novel_id,),
            ).fetchone()
            if working is None or working[0] != expected_working_plan_revision_id:
                raise PlanningAuthorityError(
                    "planning Head working revision changed before projection"
                )

            active_items = (
                self._bound_items(
                    conn,
                    novel_id=novel_id,
                    plan_revision_id=expected_active_plan_revision_id,
                )
                if expected_active_plan_revision_id
                else ()
            )
            target_items = self._bound_items(
                conn,
                novel_id=novel_id,
                plan_revision_id=plan_revision_id,
            )
            active_ids = {
                item.story_node_id for item in active_items if item.story_node_id
            }
            target_ids = {
                item.story_node_id for item in target_items if item.story_node_id
            }

            for item in active_items:
                if item.story_node_id is None:
                    continue
                existing = self._existing_node(conn, item.story_node_id)
                if existing is None:
                    raise PlanningAuthorityError(
                        "active projection StoryNode is missing"
                    )
                if (
                    str(existing["novel_id"] or "") != novel_id
                    or str(existing["node_type"] or "") != item.level
                    or existing["parent_id"] != item.parent_story_node_id
                    or int(existing["number"]) != item.number
                    or int(existing["order_index"]) != item.order_index
                ):
                    raise PlanningAuthorityError(
                        "active projection contains a forged StoryNode"
                    )

            existing_by_target: dict[str, Optional[sqlite3.Row]] = {}
            for item in target_items:
                if item.story_node_id is None:
                    continue
                existing = self._existing_node(conn, item.story_node_id)
                existing_by_target[item.story_node_id] = existing
                if existing is None:
                    if item.story_node_id in active_ids:
                        raise PlanningAuthorityError(
                            "active projection StoryNode is missing"
                        )
                    continue
                if str(existing["novel_id"] or "") != novel_id:
                    raise PlanningAuthorityError(
                        "cross-novel StoryNode occupies a projection binding"
                    )
                if item.story_node_id not in active_ids:
                    raise PlanningAuthorityError(
                        "forged StoryNode occupies a new projection binding"
                    )
                if (
                    str(existing["node_type"] or "") != item.level
                    or existing["parent_id"] != item.parent_story_node_id
                    or int(existing["number"]) != item.number
                    or int(existing["order_index"]) != item.order_index
                ):
                    raise PlanningAuthorityError(
                        "forged StoryNode does not match its projection binding"
                    )

            live_ids = {
                str(row[0])
                for row in conn.execute(
                    "SELECT id FROM story_nodes WHERE novel_id = ?", (novel_id,)
                ).fetchall()
            }
            extra_ids = live_ids - active_ids
            if extra_ids:
                raise PlanningAuthorityError(
                    "physical projection contains an extra StoryNode"
                )

            delete_ids = active_ids - target_ids
            self._assert_deletes_are_unreferenced(
                conn,
                novel_id=novel_id,
                delete_ids=delete_ids,
            )
            active_by_id = {
                item.story_node_id: item
                for item in active_items
                if item.story_node_id is not None
            }
            for story_node_id in sorted(
                delete_ids,
                key=lambda node_id: _LEVEL_DEPTH[active_by_id[node_id].level],
                reverse=True,
            ):
                deleted = self._delete_materialized_node(
                    conn,
                    capability,
                    novel_id=novel_id,
                    story_node_id=story_node_id,
                )
                if not deleted:
                    raise PlanningAuthorityError(
                        "bound projection StoryNode disappeared before deletion"
                    )

            materialized = [
                self._materialized_node(
                    item,
                    novel_id=novel_id,
                    existing_row=existing_by_target[item.story_node_id],
                    plan_revision_id=plan_revision_id,
                )
                for item in target_items
                if item.story_node_id is not None
            ]
            if materialized:
                self._save_materialized_nodes(
                    conn,
                    capability,
                    novel_id=novel_id,
                    nodes=materialized,
                )

            self._assert_materialized_projection(
                conn,
                novel_id=novel_id,
                plan_revision_id=plan_revision_id,
                items=target_items,
            )

            self._activate_bound_head(
                conn,
                capability,
                expected_working_plan_revision_id=expected_working_plan_revision_id,
            )
            _validate_projection_capability(conn, novel_id, capability)
        except BaseException:
            if session is not None:
                _end_projection_writer_session(conn, session)
            if capability is not None:
                capability._expire()
            if conn.in_transaction:
                conn.rollback()
            raise
        else:
            if session is not None:
                _end_projection_writer_session(conn, session)
            if capability is not None:
                capability._expire()

    async def apply_atomic(
        self, *, novel_id: str, plan_revision_id: str, operation: str,
        expected_active_plan_revision_id: Optional[str], expected_active_plan_digest: str,
        expected_authority_generation: int, expected_projection_generation: int,
        creates: Sequence["StoryNode"] = (), updates: Sequence["StoryNode"] = (),
        deletes: Sequence[str] = (),
    ) -> None:
        raise PlanningAuthorityError(
            "caller-supplied StoryNode projection batches are disabled"
        )
