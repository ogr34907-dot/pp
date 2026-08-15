"""Manifest cutover must close every legacy StoryNode/contract writer."""

import asyncio
import sqlite3

import pytest

from domain.structure.outline_contract import OutlinePayload
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
from infrastructure.persistence.database.sqlite_chapter_repository import SqliteChapterRepository
from application.blueprint.services.chapter_book_structure_sync import (
    purge_chapter_book_rows_not_matching_structure,
)
from application.core.services.chapter_rewrite_coordinator import ChapterRewriteCoordinator
from application.engine.services.autopilot_recovery_policy import AutopilotRecoveryPolicy


@pytest.fixture
def manifest_book(tmp_path):
    database = DatabaseConnection(str(tmp_path / "manifest-guard.db"))
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
    from domain.structure.outline_plan import OutlinePlanItem

    item = OutlinePlanItem(
        logical_node_id=published.id,
        version_id=str(row[0]),
        version_digest=str(row[1]),
        level=published.level,
        sibling_index=0,
    )
    plan = contracts.create_plan_draft(
        novel_id="novel-1",
        items=(item,),
        canonical_prefix_digest="",
        canonical_boundary={"formal_head": 0},
    )
    plan = contracts.seal_plan_revision(plan.id)
    conn.execute(
        "UPDATE outline_planning_heads SET authority_mode='manifest', "
        "authority_generation=1, projection_generation=1, "
        "active_plan_revision_id=?, active_plan_digest=? WHERE novel_id=?",
        (plan.id, plan.digest, "novel-1"),
    )
    conn.commit()
    return database, StoryNodeRepository(database), contracts, plan.id


def _node(node_id: str = "part-1") -> StoryNode:
    return StoryNode(
        id=node_id,
        novel_id="novel-1",
        node_type=NodeType.PART,
        number=1,
        title="Part",
        order_index=0,
    )


def test_legacy_story_node_writers_fail_closed(manifest_book):
    database, repository, contracts, _ = manifest_book
    with pytest.raises(PlanningAuthorityError, match="legacy StoryNode"):
        repository.save_sync(_node())

    conn = database.get_connection()
    conn.execute(
        "INSERT INTO story_nodes (id, novel_id, node_type, number, title, order_index) "
        "VALUES ('part-1', 'novel-1', 'part', 1, 'old', 0)"
    )
    conn.commit()
    existing = asyncio.run(repository.get_by_id("part-1"))
    existing.title = "changed"
    with pytest.raises(PlanningAuthorityError, match="legacy StoryNode"):
        asyncio.run(repository.update(existing))
    with pytest.raises(PlanningAuthorityError, match="legacy StoryNode"):
        asyncio.run(repository.save_batch([existing]))
    with pytest.raises(PlanningAuthorityError, match="legacy StoryNode"):
        asyncio.run(repository.apply_merge_plan([], [{"id": "part-1", "title": "x", "order_index": 0}], []))
    with pytest.raises(PlanningAuthorityError, match="legacy StoryNode"):
        asyncio.run(repository.delete("part-1"))
    with pytest.raises(PlanningAuthorityError, match="legacy StoryNode"):
        asyncio.run(repository.update_chapter_ranges("novel-1"))
    with pytest.raises(PlanningAuthorityError, match="legacy StoryNode"):
        repository.bulk_replace_text_sync("novel-1", "old", "new")


def test_projection_writer_is_the_explicit_manifest_exception(manifest_book):
    _, repository, _, plan_id = manifest_book
    writer = PlanProjectionWriter(repository)
    capability = writer.capability_for("novel-1", plan_id)
    writer.save_sync(_node(), capability)
    saved = asyncio.run(repository.get_by_id("part-1"))
    assert saved is not None
    assert saved.title == "Part"


def test_manifest_runtime_whitelist_does_not_allow_planning_changes(manifest_book):
    _, repository, _, plan_id = manifest_book
    writer = PlanProjectionWriter(repository)
    capability = writer.capability_for("novel-1", plan_id)
    writer.save_sync(_node(), capability)
    node = asyncio.run(repository.get_by_id("part-1"))
    node.word_count = 42
    node.status = "completed"
    node.metadata = {"runtime.progress": 0.5}
    asyncio.run(repository.update(node))
    refreshed = asyncio.run(repository.get_by_id("part-1"))
    assert refreshed.title == "Part"
    assert refreshed.word_count == 42
    assert refreshed.status == "completed"
    assert refreshed.metadata["runtime.progress"] == 0.5

    node.title = "must fail"
    with pytest.raises(PlanningAuthorityError):
        asyncio.run(repository.update(node))


def test_direct_chapter_delete_and_tree_purge_are_blocked(manifest_book):
    database, repository, _, plan_id = manifest_book
    writer = PlanProjectionWriter(repository)
    writer.save_sync(_node(), writer.capability_for("novel-1", plan_id))
    conn = database.get_connection()
    conn.execute(
        "INSERT INTO chapters (id, novel_id, number, title, content, status) "
        "VALUES ('chapter-1', 'novel-1', 1, 'Chapter', '', 'draft')"
    )
    conn.commit()
    chapter_repo = SqliteChapterRepository(database)
    with pytest.raises(PlanningAuthorityError, match="legacy StoryNode"):
        chapter_repo._delete_chapter_transaction_body(
            conn, "novel-1", 1, "chapter-1", "now"
        )
    with pytest.raises(PlanningAuthorityError, match="tree-driven"):
        purge_chapter_book_rows_not_matching_structure(
            repository, chapter_repo, "novel-1"
        )
    assert conn.execute(
        "SELECT COUNT(*) FROM chapters WHERE id = 'chapter-1'"
    ).fetchone()[0] == 1


def test_recovery_cleanup_skips_manifest_story_node_preplan(manifest_book):
    database, _, _, _ = manifest_book
    conn = database.get_connection()
    conn.execute(
        "INSERT INTO story_nodes (id, novel_id, node_type, number, title, order_index, "
        "outline, metadata) VALUES ('chapter-1', 'novel-1', 'chapter', 1, 'Chapter', 0, "
        "'old outline', '{\"chapter_preplan\": {\"detail_outline\": \"transient\"}}')"
    )
    conn.execute(
        "INSERT INTO chapters (id, novel_id, number, status, content) "
        "VALUES ('chapter-row-1', 'novel-1', 1, 'draft', 'draft')"
    )
    conn.commit()
    AutopilotRecoveryPolicy(database)._discard_transient_chapter_preplan("novel-1", 1)
    row = conn.execute(
        "SELECT outline, metadata FROM story_nodes WHERE id = 'chapter-1'"
    ).fetchone()
    chapter = conn.execute(
        "SELECT content, status FROM chapters WHERE id = 'chapter-row-1'"
    ).fetchone()
    assert row[0] == "old outline"
    assert "chapter_preplan" in row[1]
    assert chapter[0] == ""
    assert chapter[1] == "draft"


def test_rewrite_invalidation_does_not_mutate_manifest_projection(manifest_book):
    database, _, _, _ = manifest_book
    conn = database.get_connection()
    conn.execute(
        "INSERT INTO story_nodes (id, novel_id, node_type, number, title, order_index, "
        "metadata) VALUES ('act-1', 'novel-1', 'act', 1, 'Act', 0, "
        "'{\"summary_status\": \"ready\"}')"
    )
    conn.commit()
    coordinator = ChapterRewriteCoordinator.__new__(ChapterRewriteCoordinator)
    coordinator._invalidate_story_nodes(conn, "novel-1", 1)
    metadata = conn.execute(
        "SELECT metadata FROM story_nodes WHERE id = 'act-1'"
    ).fetchone()[0]
    assert metadata == '{"summary_status": "ready"}'


def test_legacy_outline_contract_mutators_fail_closed(manifest_book):
    _, _, contracts, _ = manifest_book
    with pytest.raises(Exception, match="manifest planning authority"):
        contracts.ensure_root("novel-1")
