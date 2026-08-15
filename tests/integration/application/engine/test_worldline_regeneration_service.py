"""Tail regeneration archives all chapter-derived facts before a new epoch starts."""

import hashlib
import json
import pytest

from infrastructure.persistence.database.connection import DatabaseConnection
from infrastructure.persistence.database.chapter_candidate_repository import (
    ChapterCandidateRepository,
)
from application.engine.services.worldline_regeneration_service import (
    WorldlineRegenerationError,
    WorldlineRegenerationService,
)


def _seed(db):
    conn = db.get_connection()
    conn.execute(
        "INSERT INTO novels (id, title, slug, target_chapters) VALUES (?, ?, ?, ?)",
        ("novel-1", "Worldline Novel", "worldline-novel", 8),
    )
    for number in (1, 2, 3):
        conn.execute(
            """
            INSERT INTO chapters (id, novel_id, number, title, content, content_sha256, content_revision, status)
            VALUES (?, 'novel-1', ?, ?, ?, ?, 1, 'completed')
            """,
            (
                f"chapter-{number}",
                number,
                f"第{number}章",
                f"正文 {number}",
                hashlib.sha256(f"正文 {number}".encode("utf-8")).hexdigest(),
            ),
        )
        conn.execute(
            """
            INSERT INTO narrative_events (event_id, novel_id, chapter_number, event_summary)
            VALUES (?, 'novel-1', ?, ?)
            """,
            (f"event-{number}", number, f"事件 {number}"),
        )
        conn.execute(
            """
            INSERT INTO memory_atoms (id, novel_id, entity_id, chapter_number, status, payload_json)
            VALUES (?, 'novel-1', 'hero', ?, 'canonical', '{}')
            """,
            (f"atom-{number}", number),
        )
    conn.commit()
    # These fixtures model an explicitly imported pre-Candidate formal prefix,
    # rather than silently treating arbitrary completed rows as authority.
    ChapterCandidateRepository(db).import_legacy_formal_history("novel-1")


def _seed_canonical_tail(db):
    conn = db.get_connection()
    conn.execute(
        "INSERT INTO bible_timeline_notes "
        "(id, novel_id, event, time_point, description, source_type, chapter_number) "
        "VALUES ('timeline-2', 'novel-1', '旧时间线', '第2章', '旧事实', 'chapter_aftermath', 2)"
    )
    conn.execute(
        "INSERT INTO bible_timeline_notes "
        "(id, novel_id, event, time_point, description, source_type, chapter_number) "
        "VALUES ('timeline-authored-2', 'novel-1', '作者设定', '第2章', '固定背景', 'bible', 2)"
    )
    conn.execute(
        "INSERT INTO character_states "
        "(character_id, novel_id, current_state_summary, last_updated_chapter) "
        "VALUES ('hero', 'novel-1', '旧状态', 2)"
    )
    conn.commit()


def test_regenerate_from_any_chapter_archives_tail_and_preserves_prefix_hash(tmp_path):
    db = DatabaseConnection(str(tmp_path / "worldline.db"))
    _seed(db)
    _seed_canonical_tail(db)
    service = WorldlineRegenerationService(db)

    preview = service.preview("novel-1", start_chapter=2, target_chapters=6)
    assert preview.current_generated_chapters == 3
    assert preview.retained_through == 1
    assert preview.archive_from == 2

    result = service.execute(
        "novel-1",
        preview_token=preview.token,
        run_mode="chapter_review",
        idempotency_key="regenerate-from-2",
    )
    conn = db.get_connection()
    assert result.archive_id
    assert conn.execute(
        "SELECT content_sha256 FROM chapters WHERE novel_id = 'novel-1' AND number = 1"
    ).fetchone()[0] == hashlib.sha256("正文 1".encode("utf-8")).hexdigest()
    assert conn.execute("SELECT COUNT(*) FROM chapters WHERE novel_id = 'novel-1'").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM narrative_events WHERE novel_id = 'novel-1'").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM memory_atoms WHERE novel_id = 'novel-1'").fetchone()[0] == 1
    assert [
        tuple(row)
        for row in conn.execute(
            "SELECT id, source_type FROM bible_timeline_notes WHERE novel_id = 'novel-1'"
        ).fetchall()
    ] == [("timeline-authored-2", "bible")]
    assert conn.execute(
        "SELECT COUNT(*) FROM character_states WHERE novel_id = 'novel-1'"
    ).fetchone()[0] == 0
    assert [
        tuple(row)
        for row in conn.execute(
            "SELECT source_table, COUNT(*) FROM worldline_archive_entries "
            "WHERE archive_id = ? AND source_table IN ('bible_timeline_notes', 'character_states') "
            "GROUP BY source_table ORDER BY source_table",
            (result.archive_id,),
        ).fetchall()
    ] == [("bible_timeline_notes", 1), ("character_states", 1)]
    assert conn.execute(
        "SELECT COUNT(*) FROM worldline_archive_entries WHERE archive_id = ? AND source_table = 'chapters'",
        (result.archive_id,),
    ).fetchone()[0] == 2
    run = conn.execute(
        """
        SELECT generation_epoch, state, run_mode, current_formal_chapter,
               canonical_sync_status, next_action
        FROM novel_generation_runs WHERE novel_id = 'novel-1'
        """
    ).fetchone()
    assert tuple(run) == (1, "paused", "chapter_review", 1, "rebuilding", "rebuild_worldline")


def test_regeneration_preserves_prefix_foreshadows_in_registry(tmp_path):
    db = DatabaseConnection(str(tmp_path / "worldline-foreshadow-prefix.db"))
    _seed(db)
    db.execute(
        "INSERT INTO novel_foreshadow_registry (novel_id, payload) VALUES (?, ?)",
        (
            "novel-1",
            json.dumps(
                {
                    "id": "registry-1",
                    "novel_id": "novel-1",
                    "foreshadowings": [
                        {
                            "id": "manual-prefix",
                            "planted_in_chapter": 1,
                            "description": "作者 Bible 中的开篇暗线",
                            "importance": 4,
                            "status": "planted",
                            "suggested_resolve_chapter": 8,
                            "resolved_in_chapter": None,
                        },
                        {
                            "id": "auto-tail",
                            "planted_in_chapter": 2,
                            "description": "重写点之后自动种下的暗线",
                            "importance": 2,
                            "status": "planted",
                            "suggested_resolve_chapter": 5,
                            "resolved_in_chapter": None,
                        },
                    ],
                    "subtext_entries": [],
                },
                ensure_ascii=False,
            ),
        ),
    )
    db.commit()

    service = WorldlineRegenerationService(db)
    preview = service.preview("novel-1", start_chapter=2, target_chapters=6)
    service.execute("novel-1", preview_token=preview.token, run_mode="chapter_review")

    payload = json.loads(
        db.fetch_one(
            "SELECT payload FROM novel_foreshadow_registry WHERE novel_id = 'novel-1'"
        )["payload"]
    )
    assert [item["id"] for item in payload["foreshadowings"]] == ["manual-prefix"]


def test_regeneration_target_must_include_the_restart_chapter(tmp_path):
    db = DatabaseConnection(str(tmp_path / "worldline-target-boundary.db"))
    _seed(db)
    service = WorldlineRegenerationService(db)

    with pytest.raises(
        WorldlineRegenerationError,
        match="target_chapters must be at least start_chapter",
    ):
        service.preview("novel-1", start_chapter=2, target_chapters=1)


def test_reset_past_the_current_tail_is_regular_continuation_not_destructive(tmp_path):
    db = DatabaseConnection(str(tmp_path / "worldline-continue.db"))
    _seed(db)
    service = WorldlineRegenerationService(db)

    preview = service.preview("novel-1", start_chapter=4, target_chapters=8)
    assert preview.operation == "continue"
    result = service.execute("novel-1", preview_token=preview.token, run_mode="continuous")

    assert result.operation == "continue"
    assert db.get_connection().execute("SELECT COUNT(*) FROM chapters WHERE novel_id = 'novel-1'").fetchone()[0] == 3


def test_empty_draft_does_not_extend_worldline_formal_head(tmp_path):
    db = DatabaseConnection(str(tmp_path / "worldline-empty-draft.db"))
    _seed(db)
    db.execute(
        "INSERT INTO chapters (id, novel_id, number, title, content, status) "
        "VALUES ('draft-4', 'novel-1', 4, '第四章', '', 'draft')"
    )
    db.get_connection().commit()
    service = WorldlineRegenerationService(db)

    preview = service.preview("novel-1", start_chapter=4, target_chapters=8)

    assert preview.operation == "continue"
    assert preview.current_generated_chapters == 3
    assert preview.retained_through == 3
    assert preview.archive_from is None
    assert preview.archive_to is None
    result = service.execute(
        "novel-1",
        preview_token=preview.token,
        run_mode="chapter_review",
    )
    assert result.operation == "continue"
    assert result.retained_through == 3


def test_continue_preview_is_consumed_after_execution(tmp_path):
    db = DatabaseConnection(str(tmp_path / "worldline-continue-once.db"))
    _seed(db)
    service = WorldlineRegenerationService(db)
    preview = service.preview("novel-1", start_chapter=4, target_chapters=8)

    service.execute("novel-1", preview_token=preview.token, run_mode="continuous")

    with pytest.raises(WorldlineRegenerationError, match="already consumed"):
        service.execute("novel-1", preview_token=preview.token, run_mode="chapter_review")


def test_continue_preview_revalidates_the_existing_chapter_prefix(tmp_path):
    db = DatabaseConnection(str(tmp_path / "worldline-continue-stale.db"))
    _seed(db)
    service = WorldlineRegenerationService(db)
    preview = service.preview("novel-1", start_chapter=4, target_chapters=8)
    db.execute(
        "UPDATE chapters SET content = '已修改正文', content_sha256 = 'changed' "
        "WHERE novel_id = 'novel-1' AND number = 1"
    )
    db.get_connection().commit()

    with pytest.raises(WorldlineRegenerationError, match="chapter prefix changed"):
        service.execute("novel-1", preview_token=preview.token, run_mode="continuous")


def test_restore_archived_worldline_replaces_new_tail_and_creates_a_new_epoch(tmp_path):
    db = DatabaseConnection(str(tmp_path / "worldline-restore.db"))
    _seed(db)
    service = WorldlineRegenerationService(db)

    preview = service.preview("novel-1", start_chapter=2, target_chapters=6)
    archived = service.execute(
        "novel-1",
        preview_token=preview.token,
        run_mode="continuous",
        idempotency_key="archive-original-tail",
    )
    conn = db.get_connection()
    conn.execute(
        """
        INSERT INTO chapters (id, novel_id, number, title, content, content_sha256, content_revision, status)
        VALUES ('replacement-2', 'novel-1', 2, '新世界线第2章', '新正文', 'new-hash-2', 1, 'completed')
        """
    )
    conn.commit()

    restored = service.restore("novel-1", archive_id=archived.archive_id, run_mode="chapter_review")

    assert restored.archive_id != archived.archive_id
    assert restored.generation_epoch == 2
    rows = conn.execute(
        "SELECT number, content_sha256 FROM chapters WHERE novel_id = 'novel-1' ORDER BY number"
    ).fetchall()
    assert [tuple(row) for row in rows] == [
        (number, hashlib.sha256(f"正文 {number}".encode("utf-8")).hexdigest())
        for number in (1, 2, 3)
    ]
    assert conn.execute(
        "SELECT status FROM worldline_archives WHERE id = ?", (archived.archive_id,)
    ).fetchone()[0] == "restored"
