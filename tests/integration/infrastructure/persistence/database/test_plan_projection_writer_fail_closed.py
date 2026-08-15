"""Caller-supplied StoryNode projection batches are unsafe until declared."""

from __future__ import annotations

import asyncio

import pytest

from domain.structure.outline_contract import OutlinePayload
from domain.structure.outline_plan import OutlinePlanItem
from domain.structure.story_node import NodeType, StoryNode
from infrastructure.persistence.database.connection import DatabaseConnection
from infrastructure.persistence.database.outline_contract_repository import (
    OutlineContractRepository,
)
from infrastructure.persistence.database.plan_projection_writer import PlanProjectionWriter
from infrastructure.persistence.database.planning_authority_guard import (
    PlanningAuthorityError,
)
from infrastructure.persistence.database.story_node_repository import StoryNodeRepository


def _sealed_plan(tmp_path, *, manifest: bool) -> tuple[
    DatabaseConnection, StoryNodeRepository, str
]:
    database = DatabaseConnection(str(tmp_path / "projection-disabled.db"))
    conn = database.get_connection()
    conn.execute(
        "INSERT INTO novels (id, title, slug, target_chapters) VALUES (?, ?, ?, ?)",
        ("novel-1", "Manifest", "manifest", 20),
    )
    conn.commit()
    contracts = OutlineContractRepository(database)
    root = contracts.ensure_root("novel-1")
    draft = contracts.save_draft(
        root.id,
        OutlinePayload(
            title="Root",
            narrative_text="A complete premise",
            creative_goal="Reach the irreversible ending",
            entry_state="start",
            exit_state="end",
        ),
    )
    published = contracts.publish_and_sync(
        root.id, expected_revision=draft.draft.revision, idempotency_key="root"
    )
    row = conn.execute(
        "SELECT active_version_id, digest FROM outline_contracts "
        "JOIN outline_contract_versions ON outline_contract_versions.id = active_version_id "
        "WHERE outline_contracts.id = ?",
        (published.id,),
    ).fetchone()
    plan = contracts.create_plan_draft(
        novel_id="novel-1",
        items=(
            OutlinePlanItem(
                logical_node_id=published.id,
                version_id=str(row[0]),
                version_digest=str(row[1]),
                level=published.level,
                sibling_index=0,
            ),
        ),
        canonical_prefix_digest="",
        canonical_boundary={"formal_head": 0},
    )
    plan = contracts.seal_plan_revision(plan.id)
    if manifest:
        conn.execute(
            "UPDATE outline_planning_heads SET authority_mode = 'manifest', "
            "authority_generation = 1, projection_generation = 1, "
            "active_plan_revision_id = ?, active_plan_digest = ? "
            "WHERE novel_id = ?",
            (plan.id, plan.digest, "novel-1"),
        )
        conn.commit()
    return database, StoryNodeRepository(database), plan.id


def _part() -> StoryNode:
    return StoryNode(
        id="part-1",
        novel_id="novel-1",
        node_type=NodeType.PART,
        number=1,
        title="Part",
        order_index=0,
    )


def _head(database: DatabaseConnection) -> tuple[object, ...]:
    row = database.get_connection().execute(
        """
        SELECT authority_mode, active_plan_revision_id, active_plan_digest,
               authority_generation, projection_generation
        FROM outline_planning_heads WHERE novel_id = 'novel-1'
        """
    ).fetchone()
    return tuple(row)


class _HostileRepository(StoryNodeRepository):
    def __init__(self, database: DatabaseConnection) -> None:
        super().__init__(database)
        self.save_batch_calls = 0

    async def save_batch(self, nodes, *, _capability=None):  # type: ignore[no-untyped-def]
        self.save_batch_calls += 1
        return await super().save_batch(nodes, _capability=_capability)


def test_projection_writer_rejects_caller_batch_before_repository_dml(tmp_path):
    database, _, plan_id = _sealed_plan(tmp_path, manifest=True)
    repository = _HostileRepository(database)
    writer = PlanProjectionWriter(repository)
    before = _head(database)

    with pytest.raises(PlanningAuthorityError, match="designated Head operation"):
        asyncio.run(
            writer.apply_atomic(
                novel_id="novel-1",
                plan_revision_id=plan_id,
                operation="projection",
                expected_active_plan_revision_id=plan_id,
                expected_active_plan_digest=str(before[2]),
                expected_authority_generation=int(before[3]),
                expected_projection_generation=int(before[4]),
                creates=(_part(),),
            )
        )

    assert repository.save_batch_calls == 0
    assert database.get_connection().execute(
        "SELECT COUNT(*) FROM story_nodes WHERE id = 'part-1'"
    ).fetchone()[0] == 0
    assert _head(database) == before


def test_projection_writer_rejects_empty_cutover_without_switching_head(tmp_path):
    database, repository, plan_id = _sealed_plan(tmp_path, manifest=False)
    writer = PlanProjectionWriter(repository)
    before = _head(database)

    with pytest.raises(PlanningAuthorityError, match="declared physical projection change"):
        asyncio.run(
            writer.apply_atomic(
                novel_id="novel-1",
                plan_revision_id=plan_id,
                operation="cutover",
                expected_active_plan_revision_id=None,
                expected_active_plan_digest="",
                expected_authority_generation=0,
                expected_projection_generation=0,
            )
        )

    assert _head(database) == before


def test_projection_permit_helpers_require_a_clean_connection(
    tmp_path,
):
    database, _, _ = _sealed_plan(tmp_path, manifest=True)
    conn = database.get_connection()
    from infrastructure.persistence.database.planning_authority_guard import (
        _begin_projection_writer_session,
    )

    conn.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(PlanningAuthorityError, match="clean connection"):
            _begin_projection_writer_session(conn)
    finally:
        if conn.in_transaction:
            conn.rollback()
