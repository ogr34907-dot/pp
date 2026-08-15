import hashlib
import json

from application.core.services.chapter_rewrite_coordinator import ChapterRewriteCoordinator
from application.engine.services.context_budget_allocator import ContextBudgetAllocator
from domain.novel.value_objects.novel_id import NovelId
from infrastructure.persistence.database.connection import DatabaseConnection
from infrastructure.persistence.database.sqlite_chapter_repository import (
    SqliteChapterRepository,
)
from infrastructure.persistence.database.story_node_repository import StoryNodeRepository


def _manifest_book_with_runtime_summary(tmp_path):
    database = DatabaseConnection(str(tmp_path / "manifest-runtime-rewrite.db"))
    conn = database.get_connection()
    conn.execute(
        "INSERT INTO novels (id, title, slug, target_chapters) VALUES "
        "('novel-1', 'Manifest Rewrite', 'manifest-rewrite', 4)"
    )
    conn.execute(
        "INSERT INTO outline_plan_revisions "
        "(id, novel_id, revision, status, digest, canonical_prefix_digest, "
        "reconciliation_status, sealed_at) "
        "VALUES ('plan-1', 'novel-1', 1, 'ready_for_review', 'plan-digest', '', "
        "'aligned', CURRENT_TIMESTAMP)"
    )
    conn.execute(
        "INSERT INTO outline_planning_heads "
        "(novel_id, authority_mode, authority_generation, active_plan_revision_id, "
        "active_plan_digest, projection_generation) "
        "VALUES ('novel-1', 'manifest', 1, 'plan-1', 'plan-digest', 1)"
    )
    metadata = {
        "planning.keep": "immutable projection payload",
        "runtime.summary": "old macro summary",
        "runtime.summary_state": {
            "status": "committed",
            "chapter_start": 1,
            "chapter_end": 2,
            "source_chapter_numbers": [1, 2],
            "source_version": "old-source",
            "pipeline_version": "node-summary/v1",
        },
    }
    conn.execute(
        "INSERT INTO story_nodes "
        "(id, novel_id, node_type, number, title, description, order_index, "
        "chapter_start, chapter_end, metadata) "
        "VALUES ('act-1', 'novel-1', 'act', 1, 'Act title', 'Planning synopsis', "
        "1, 1, 2, ?)",
        (json.dumps(metadata),),
    )
    conn.commit()
    return database


def _legacy_book_with_runtime_summaries(tmp_path):
    database = DatabaseConnection(str(tmp_path / "legacy-runtime-rewrite.db"))
    conn = database.get_connection()
    old_content = "legacy formal chapter"
    conn.execute(
        "INSERT INTO novels (id, title, slug, target_chapters) VALUES "
        "('novel-1', 'Legacy Rewrite', 'legacy-rewrite', 4)"
    )
    conn.execute(
        "INSERT INTO chapters "
        "(id, novel_id, number, title, content, content_sha256, content_revision, status) "
        "VALUES ('chapter-2', 'novel-1', 2, 'Chapter 2', ?, ?, 1, 'completed')",
        (old_content, hashlib.sha256(old_content.encode("utf-8")).hexdigest()),
    )
    conn.execute(
        "INSERT INTO story_nodes "
        "(id, novel_id, node_type, number, title, description, order_index, "
        "chapter_start, chapter_end, metadata) "
        "VALUES ('act-1', 'novel-1', 'act', 1, 'Act title', 'Planning synopsis', "
        "1, 1, 2, ?)",
        (
            json.dumps(
                {
                    "planning.keep": "legacy planning payload",
                    "runtime.summary": "old runtime macro summary",
                    "runtime.summary_state": {
                        "status": "committed",
                        "chapter_start": 1,
                        "chapter_end": 2,
                        "source_chapter_numbers": [1, 2],
                        "source_version": "old-runtime-source",
                    },
                    "summary": "old legacy macro fallback",
                    "summary_state": {
                        "status": "committed",
                        "chapter_start": 1,
                        "chapter_end": 2,
                        "source_version": "old-legacy-source",
                    },
                }
            ),
        ),
    )
    conn.execute(
        "INSERT INTO story_nodes "
        "(id, novel_id, parent_id, node_type, number, title, order_index, metadata) "
        "VALUES ('chapter-node-2', 'novel-1', 'act-1', 'chapter', 2, 'Chapter node', 2, ?)",
        (
            json.dumps(
                {
                    "planning.keep": "chapter planning payload",
                    "runtime.checkpoint_summary": "old runtime checkpoint",
                    "runtime.checkpoint_summary_state": {
                        "status": "committed",
                        "chapter_start": 2,
                        "chapter_end": 2,
                        "source_chapter_numbers": [2],
                        "source_version": "old-runtime-source",
                    },
                    "checkpoint_summary": "old legacy checkpoint fallback",
                    "checkpoint_summary_state": {
                        "status": "committed",
                        "chapter_start": 2,
                        "chapter_end": 2,
                        "source_version": "old-legacy-source",
                    },
                }
            ),
        ),
    )
    conn.commit()
    return database


def test_manifest_rewrite_invalidates_only_runtime_summary_in_caller_transaction(tmp_path):
    database = _manifest_book_with_runtime_summary(tmp_path)
    conn = database.get_connection()
    coordinator = ChapterRewriteCoordinator(db=database, chapter_repository=None)

    conn.execute("BEGIN IMMEDIATE")
    coordinator._invalidate_story_nodes(conn, "novel-1", 2)

    assert conn.in_transaction is True
    row = conn.execute(
        "SELECT title, description, metadata FROM story_nodes WHERE id = 'act-1'"
    ).fetchone()
    metadata = json.loads(row[2])
    assert row[0:2] == ("Act title", "Planning synopsis")
    assert metadata["planning.keep"] == "immutable projection payload"
    assert "runtime.summary" not in metadata
    assert "runtime.summary_state" not in metadata
    assert metadata["runtime.summary_invalidated_from_chapter"] == 2
    conn.rollback()


def test_confirmed_manifest_rewrite_stales_runtime_cache_with_the_chapter_update(tmp_path):
    database = _manifest_book_with_runtime_summary(tmp_path)
    conn = database.get_connection()
    old_content = "old formal chapter"
    conn.execute(
        "INSERT INTO chapters "
        "(id, novel_id, number, title, content, content_sha256, content_revision, status) "
        "VALUES ('chapter-2', 'novel-1', 2, 'Chapter 2', ?, ?, 1, 'completed')",
        (old_content, hashlib.sha256(old_content.encode("utf-8")).hexdigest()),
    )
    conn.commit()
    chapters = SqliteChapterRepository(database)
    chapter = chapters.get_by_novel_and_number(NovelId("novel-1"), 2)

    result = ChapterRewriteCoordinator(
        db=database,
        chapter_repository=chapters,
    ).rewrite(chapter, "new formal chapter")

    assert result.requires_rebuild is True
    row = conn.execute(
        "SELECT content, content_revision FROM chapters WHERE id = 'chapter-2'"
    ).fetchone()
    metadata = json.loads(
        conn.execute("SELECT metadata FROM story_nodes WHERE id = 'act-1'").fetchone()[0]
    )
    assert tuple(row) == ("new formal chapter", 2)
    assert "runtime.summary" not in metadata
    assert "runtime.summary_state" not in metadata
    assert metadata["runtime.summary_invalidated_from_chapter"] == 2


def test_legacy_rewrite_invalidates_runtime_caches_and_fences_legacy_fallbacks(tmp_path):
    database = _legacy_book_with_runtime_summaries(tmp_path)
    conn = database.get_connection()
    chapters = SqliteChapterRepository(database)
    chapter = chapters.get_by_novel_and_number(NovelId("novel-1"), 2)

    ChapterRewriteCoordinator(
        db=database,
        chapter_repository=chapters,
    ).rewrite(chapter, "rewritten legacy formal chapter")

    nodes = {
        node.id: node
        for node in StoryNodeRepository(database).get_by_novel_sync("novel-1")
    }
    act = nodes["act-1"]
    checkpoint = nodes["chapter-node-2"]

    assert act.metadata["planning.keep"] == "legacy planning payload"
    assert "runtime.summary" not in act.metadata
    assert "runtime.summary_state" not in act.metadata
    assert act.metadata["runtime.summary_invalidated_from_chapter"] == 2
    assert checkpoint.metadata["planning.keep"] == "chapter planning payload"
    assert "runtime.checkpoint_summary" not in checkpoint.metadata
    assert "runtime.checkpoint_summary_state" not in checkpoint.metadata
    assert checkpoint.metadata["runtime.checkpoint_summary_invalidated_from_chapter"] == 2

    allocator = ContextBudgetAllocator(
        story_node_repository=StoryNodeRepository(database),
        chapter_repository=chapters,
    )
    assert allocator._get_valid_node_summary("novel-1", act) == ""
    assert (
        allocator._get_latest_valid_checkpoint_summary("novel-1", [checkpoint], 3)
        == ""
    )
