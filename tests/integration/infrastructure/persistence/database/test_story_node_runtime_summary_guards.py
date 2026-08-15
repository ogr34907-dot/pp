import hashlib
import json

import pytest

from application.world.services.chapter_narrative_sync import (
    CHAPTER_NARRATIVE_PIPELINE_VERSION,
)
from infrastructure.persistence.database.connection import DatabaseConnection
from infrastructure.persistence.database.story_node_repository import StoryNodeRepository


def _summary_state(*, number: int, content: str, revision: int) -> dict[str, object]:
    content_sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()
    payload = f"{number}:{content_sha256}:{revision}"
    return {
        "status": "committed",
        "chapter_start": number,
        "chapter_end": number,
        "source_chapter_numbers": [number],
        "source_version": hashlib.sha256(payload.encode("utf-8")).hexdigest(),
        "pipeline_version": "node-summary/v1",
    }


def _insert_candidate_formal_source(
    conn,
    *,
    content: str,
    include_durable_aftermath: bool,
) -> str:
    content_sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()
    conn.execute(
        "INSERT INTO chapters "
        "(id, novel_id, number, title, content, content_sha256, content_revision, status) "
        "VALUES ('chapter-1', 'novel-1', 1, 'Chapter 1', ?, ?, 1, 'completed')",
        (content, content_sha256),
    )
    conn.execute(
        "INSERT INTO chapter_candidates "
        "(id, novel_id, chapter_number, generation_epoch, status) "
        "VALUES ('candidate-1', 'novel-1', 1, 0, 'committed')"
    )
    conn.execute(
        "INSERT INTO chapter_candidate_formal_commits "
        "(candidate_id, novel_id, chapter_number, chapter_id, content_sha256, "
        "content_revision, sync_status) "
        "VALUES ('candidate-1', 'novel-1', 1, 'chapter-1', ?, 1, 'ready')",
        (content_sha256,),
    )
    if include_durable_aftermath:
        conn.execute(
            "INSERT INTO knowledge (id, novel_id) VALUES ('knowledge-1', 'novel-1')"
        )
        conn.execute(
            """
            INSERT INTO chapter_narrative_commits
                (novel_id, chapter_number, content_sha256, pipeline_version,
                 content_revision, status, memory_status)
            VALUES ('novel-1', 1, ?, ?, 1, 'committed', 'committed')
            """,
            (content_sha256, CHAPTER_NARRATIVE_PIPELINE_VERSION),
        )
        conn.execute(
            """
            INSERT INTO chapter_summaries
                (id, knowledge_id, chapter_number, summary, source_content_sha256,
                 source_content_revision, pipeline_version, sync_status)
            VALUES ('summary-1', 'knowledge-1', 1, 'Canonical summary', ?, 1, ?, 'committed')
            """,
            (content_sha256, CHAPTER_NARRATIVE_PIPELINE_VERSION),
        )
    return content_sha256


def _insert_legacy_durable_aftermath(conn, *, content_sha256: str) -> None:
    """Supply the same exact aftermath proof required of migrated Formal prose."""

    conn.execute(
        "INSERT INTO knowledge (id, novel_id) VALUES ('knowledge-1', 'novel-1')"
    )
    conn.execute(
        """
        INSERT INTO chapter_narrative_commits
            (novel_id, chapter_number, content_sha256, pipeline_version,
             content_revision, status, memory_status)
        VALUES ('novel-1', 1, ?, ?, 1, 'committed', 'committed')
        """,
        (content_sha256, CHAPTER_NARRATIVE_PIPELINE_VERSION),
    )
    conn.execute(
        """
        INSERT INTO chapter_summaries
            (id, knowledge_id, chapter_number, summary, source_content_sha256,
             source_content_revision, pipeline_version, sync_status)
        VALUES ('legacy-summary-1', 'knowledge-1', 1, 'Canonical legacy summary',
                ?, 1, ?, 'committed')
        """,
        (content_sha256, CHAPTER_NARRATIVE_PIPELINE_VERSION),
    )


def test_runtime_summary_patch_rejects_source_revision_changed_before_write(tmp_path):
    database = DatabaseConnection(str(tmp_path / "runtime-summary-source-cas.db"))
    conn = database.get_connection()
    conn.execute(
        "INSERT INTO novels (id, title, slug, target_chapters) VALUES "
        "('novel-1', 'Runtime Summary', 'runtime-summary', 3)"
    )
    old_content = "old canonical prose"
    old_hash = hashlib.sha256(old_content.encode("utf-8")).hexdigest()
    conn.execute(
        "INSERT INTO chapters "
        "(id, novel_id, number, title, content, content_sha256, content_revision, status) "
        "VALUES ('chapter-1', 'novel-1', 1, 'Chapter 1', ?, ?, 1, 'completed')",
        (old_content, old_hash),
    )
    conn.execute(
        "INSERT INTO story_nodes "
        "(id, novel_id, node_type, number, title, order_index, metadata) "
        "VALUES ('act-1', 'novel-1', 'act', 1, 'Act 1', 1, '{}')"
    )
    new_content = "rewritten canonical prose"
    conn.execute(
        "UPDATE chapters SET content = ?, content_sha256 = ?, content_revision = 2 "
        "WHERE id = 'chapter-1'",
        (new_content, hashlib.sha256(new_content.encode("utf-8")).hexdigest()),
    )
    conn.commit()

    result = StoryNodeRepository(database).update_runtime_fields(
        "act-1",
        runtime_metadata={
            "runtime.summary": "summary generated from the old body",
            "runtime.summary_state": _summary_state(
                number=1,
                content=old_content,
                revision=1,
            ),
        },
        _verify_summary_sources=True,
    )

    assert result is False
    metadata = json.loads(
        conn.execute("SELECT metadata FROM story_nodes WHERE id = 'act-1'").fetchone()[0]
    )
    assert metadata == {}


def test_runtime_summary_patch_rejects_completed_chapter_without_durable_aftermath(
    tmp_path,
):
    database = DatabaseConnection(str(tmp_path / "runtime-summary-unproven-formal.db"))
    conn = database.get_connection()
    conn.execute(
        "INSERT INTO novels (id, title, slug, target_chapters) VALUES "
        "('novel-1', 'Runtime Summary', 'runtime-summary-unproven', 3)"
    )
    content = "completed prose that never completed canonical aftermath"
    content_sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()
    conn.execute(
        "INSERT INTO chapters "
        "(id, novel_id, number, title, content, content_sha256, content_revision, status) "
        "VALUES ('chapter-1', 'novel-1', 1, 'Chapter 1', ?, ?, 1, 'completed')",
        (content, content_sha256),
    )
    conn.execute(
        "INSERT INTO story_nodes "
        "(id, novel_id, node_type, number, title, order_index, metadata) "
        "VALUES ('act-1', 'novel-1', 'act', 1, 'Act 1', 1, '{}')"
    )
    conn.commit()

    result = StoryNodeRepository(database).update_runtime_fields(
        "act-1",
        runtime_metadata={
            "runtime.summary": "must not make unproven prose visible",
            "runtime.summary_state": _summary_state(
                number=1,
                content=content,
                revision=1,
            ),
        },
        _verify_summary_sources=True,
    )

    assert result is False
    metadata = json.loads(
        conn.execute("SELECT metadata FROM story_nodes WHERE id = 'act-1'").fetchone()[0]
    )
    assert metadata == {}


def test_runtime_summary_patch_rejects_formal_candidate_before_memory_is_ready(
    tmp_path,
):
    database = DatabaseConnection(str(tmp_path / "runtime-summary-memory-pending.db"))
    conn = database.get_connection()
    conn.execute(
        "INSERT INTO novels (id, title, slug, target_chapters) VALUES "
        "('novel-1', 'Runtime Summary', 'runtime-summary-memory-pending', 3)"
    )
    content = "formal prose whose canonical aftermath is still pending"
    _insert_candidate_formal_source(
        conn,
        content=content,
        include_durable_aftermath=False,
    )
    conn.execute(
        "INSERT INTO story_nodes "
        "(id, novel_id, node_type, number, title, order_index, metadata) "
        "VALUES ('act-1', 'novel-1', 'act', 1, 'Act 1', 1, '{}')"
    )
    conn.commit()

    result = StoryNodeRepository(database).update_runtime_fields(
        "act-1",
        runtime_metadata={
            "runtime.summary": "must wait for exact canonical and memory evidence",
            "runtime.summary_state": _summary_state(
                number=1,
                content=content,
                revision=1,
            ),
        },
        _verify_summary_sources=True,
    )

    assert result is False
    metadata = json.loads(
        conn.execute("SELECT metadata FROM story_nodes WHERE id = 'act-1'").fetchone()[0]
    )
    assert metadata == {}


def test_runtime_summary_patch_accepts_exact_legacy_formal_baseline(tmp_path):
    database = DatabaseConnection(str(tmp_path / "runtime-summary-legacy-baseline.db"))
    conn = database.get_connection()
    conn.execute(
        "INSERT INTO novels (id, title, slug, target_chapters) VALUES "
        "('novel-1', 'Runtime Summary', 'runtime-summary-legacy', 3)"
    )
    content = "legacy formal prose"
    content_sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()
    conn.execute(
        "INSERT INTO chapters "
        "(id, novel_id, number, title, content, content_sha256, content_revision, status) "
        "VALUES ('chapter-1', 'novel-1', 1, 'Chapter 1', ?, ?, 1, 'completed')",
        (content, content_sha256),
    )
    conn.execute(
        "INSERT INTO pre_candidate_formal_history "
        "(novel_id, chapter_number, chapter_id, content_sha256, content_revision) "
        "VALUES ('novel-1', 1, 'chapter-1', ?, 1)",
        (content_sha256,),
    )
    _insert_legacy_durable_aftermath(conn, content_sha256=content_sha256)
    conn.execute(
        "INSERT INTO story_nodes "
        "(id, novel_id, node_type, number, title, order_index, metadata) "
        "VALUES ('act-1', 'novel-1', 'act', 1, 'Act 1', 1, '{}')"
    )
    conn.commit()

    result = StoryNodeRepository(database).update_runtime_fields(
        "act-1",
        runtime_metadata={
            "runtime.summary": "summary from validated legacy prose",
            "runtime.summary_state": _summary_state(
                number=1,
                content=content,
                revision=1,
            ),
        },
        _verify_summary_sources=True,
    )

    assert result is True


@pytest.mark.parametrize(
    ("summary_key", "state_key"),
    [
        ("summary", "summary_state"),
        ("checkpoint_summary", "checkpoint_summary_state"),
    ],
)
def test_legacy_summary_visibility_retains_compatibility_selector(
    tmp_path, summary_key, state_key
):
    """Pure Legacy authority preserves its established committed-cache reader."""

    database = DatabaseConnection(str(tmp_path / f"legacy-{summary_key}.db"))
    conn = database.get_connection()
    conn.execute(
        "INSERT INTO novels (id, title, slug, target_chapters) VALUES "
        "('novel-1', 'Runtime Summary', 'legacy-summary', 3)"
    )
    content = "legacy formal prose"
    content_sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()
    summary_state = _summary_state(number=1, content=content, revision=1)
    conn.execute(
        "INSERT INTO chapters "
        "(id, novel_id, number, title, content, content_sha256, content_revision, status) "
        "VALUES ('chapter-1', 'novel-1', 1, 'Chapter 1', ?, ?, 1, 'completed')",
        (content, content_sha256),
    )
    conn.execute(
        "INSERT INTO pre_candidate_formal_history "
        "(novel_id, chapter_number, chapter_id, content_sha256, content_revision) "
        "VALUES ('novel-1', 1, 'chapter-1', ?, 1)",
        (content_sha256,),
    )
    conn.execute(
        "INSERT INTO story_nodes "
        "(id, novel_id, node_type, number, title, order_index, metadata) "
        "VALUES ('act-1', 'novel-1', 'act', 1, 'Act 1', 1, ?)",
        (json.dumps({summary_key: "bare legacy cache", state_key: summary_state}),),
    )
    conn.commit()
    repository = StoryNodeRepository(database)
    node = repository.get_by_novel_sync("novel-1")[0]

    assert repository.visible_summary_metadata_pairs(
        node, summary_key=summary_key, state_key=state_key
    ) == (("bare legacy cache", summary_state),)


def test_runtime_summary_patch_accepts_exact_candidate_durable_aftermath(tmp_path):
    database = DatabaseConnection(str(tmp_path / "runtime-summary-candidate-ready.db"))
    conn = database.get_connection()
    conn.execute(
        "INSERT INTO novels (id, title, slug, target_chapters) VALUES "
        "('novel-1', 'Runtime Summary', 'runtime-summary-candidate-ready', 3)"
    )
    content = "candidate formal prose with durable aftermath"
    _insert_candidate_formal_source(
        conn,
        content=content,
        include_durable_aftermath=True,
    )
    conn.execute(
        "INSERT INTO story_nodes "
        "(id, novel_id, node_type, number, title, order_index, metadata) "
        "VALUES ('act-1', 'novel-1', 'act', 1, 'Act 1', 1, '{}')"
    )
    conn.commit()

    result = StoryNodeRepository(database).update_runtime_fields(
        "act-1",
        runtime_metadata={
            "runtime.summary": "summary from exact durable aftermath",
            "runtime.summary_state": _summary_state(
                number=1,
                content=content,
                revision=1,
            ),
        },
        _verify_summary_sources=True,
    )

    assert result is True


def test_runtime_summary_invalidation_fails_closed_for_unproven_parent_provenance(tmp_path):
    database = DatabaseConnection(str(tmp_path / "runtime-summary-unproven-parent.db"))
    conn = database.get_connection()
    conn.execute(
        "INSERT INTO novels (id, title, slug, target_chapters) VALUES "
        "('novel-1', 'Runtime Summary', 'runtime-summary-parent', 3)"
    )
    conn.execute(
        "INSERT INTO story_nodes "
        "(id, novel_id, node_type, number, title, order_index, metadata) "
        "VALUES ('part-1', 'novel-1', 'part', 1, 'Part 1', 1, ?)",
        (
            json.dumps(
                {
                    "runtime.summary": "summary with no provable chapter boundary",
                }
            ),
        ),
    )
    conn.commit()

    StoryNodeRepository(database).invalidate_runtime_summary_caches("novel-1", 2)

    metadata = json.loads(
        conn.execute("SELECT metadata FROM story_nodes WHERE id = 'part-1'").fetchone()[0]
    )
    assert metadata["runtime.summary_invalidated_from_chapter"] == 2
