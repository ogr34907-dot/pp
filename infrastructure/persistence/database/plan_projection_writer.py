"""Atomic Manifest Head and physical StoryNode projection writer."""

from __future__ import annotations

import sqlite3
from collections.abc import Sequence
from contextlib import contextmanager
from typing import TYPE_CHECKING, Iterator, Optional

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
    from domain.structure.story_node import StoryNode
    from infrastructure.persistence.database.story_node_repository import StoryNodeRepository


class PlanProjectionWriter:
    """Keep projection DML and the book-level Head in one SQLite transaction."""

    def __init__(self, repository: "StoryNodeRepository") -> None:
        self._repository = repository

    @contextmanager
    def projection_transaction(
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

    def activate_head(self, capability: ProjectionWriteCapability) -> None:
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

    async def apply_atomic(
        self, *, novel_id: str, plan_revision_id: str, operation: str,
        expected_active_plan_revision_id: Optional[str], expected_active_plan_digest: str,
        expected_authority_generation: int, expected_projection_generation: int,
        creates: Sequence["StoryNode"] = (), updates: Sequence["StoryNode"] = (),
        deletes: Sequence[str] = (),
    ) -> None:
        with self.projection_transaction(
            novel_id=novel_id, plan_revision_id=plan_revision_id, operation=operation,
            expected_active_plan_revision_id=expected_active_plan_revision_id,
            expected_active_plan_digest=expected_active_plan_digest,
            expected_authority_generation=expected_authority_generation,
            expected_projection_generation=expected_projection_generation,
        ) as capability:
            nodes = [*creates, *updates]
            if nodes:
                await self._repository.save_batch(nodes, _capability=capability)
            for node_id in deletes:
                await self._repository.delete(node_id, _capability=capability)
            self.activate_head(capability)
