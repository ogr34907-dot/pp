"""Manifest authority must protect bulk StoryNode deletion too."""

import asyncio

import pytest

from domain.structure.outline_contract import OutlinePayload
from domain.structure.outline_plan import OutlinePlanItem
from infrastructure.persistence.database.connection import DatabaseConnection
from infrastructure.persistence.database.outline_contract_repository import (
    OutlineContractRepository,
)
from infrastructure.persistence.database.planning_authority_guard import (
    PlanningAuthorityError,
)
from infrastructure.persistence.database.story_node_repository import StoryNodeRepository


def _manifest_book(tmp_path):
    database = DatabaseConnection(str(tmp_path / "story-node-delete-guard.db"))
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
            creative_goal="Reach the ending",
            entry_state="start",
            exit_state="end",
        ),
    )
    published = contracts.publish_and_sync(root.id, expected_revision=draft.draft.revision)
    version = conn.execute(
        """
        SELECT version.id, version.digest
        FROM outline_contracts AS contract
        JOIN outline_contract_versions AS version ON version.id = contract.active_version_id
        WHERE contract.id = ?
        """,
        (published.id,),
    ).fetchone()
    plan = contracts.create_plan_draft(
        novel_id="novel-1",
        items=(
            OutlinePlanItem(
                logical_node_id=published.id,
                version_id=str(version[0]),
                version_digest=str(version[1]),
                level=published.level,
                sibling_index=0,
            ),
        ),
        canonical_prefix_digest="",
        canonical_boundary={"formal_head": 0},
    )
    plan = contracts.seal_plan_revision(plan.id)
    conn.execute(
        """
        UPDATE outline_planning_heads
        SET authority_mode = 'manifest', authority_generation = 1,
            active_plan_revision_id = ?, active_plan_digest = ?,
            projection_generation = 1
        WHERE novel_id = ?
        """,
        (plan.id, plan.digest, "novel-1"),
    )
    conn.execute(
        """
        INSERT INTO story_nodes (id, novel_id, node_type, number, title, order_index)
        VALUES ('part-1', 'novel-1', 'part', 1, 'Part', 0)
        """
    )
    conn.commit()
    return database, StoryNodeRepository(database)


def test_manifest_authority_blocks_delete_by_novel(tmp_path):
    database, repository = _manifest_book(tmp_path)

    with pytest.raises(PlanningAuthorityError, match="legacy StoryNode"):
        asyncio.run(repository.delete_by_novel("novel-1"))

    count = database.get_connection().execute(
        "SELECT COUNT(*) FROM story_nodes WHERE novel_id = 'novel-1'"
    ).fetchone()[0]
    assert count == 1
