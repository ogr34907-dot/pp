"""Tail regeneration archives all chapter-derived facts before a new epoch starts."""

from infrastructure.persistence.database.connection import DatabaseConnection
from application.engine.services.worldline_regeneration_service import WorldlineRegenerationService


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
            (f"chapter-{number}", number, f"第{number}章", f"正文 {number}", f"hash-{number}"),
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


def test_regenerate_from_any_chapter_archives_tail_and_preserves_prefix_hash(tmp_path):
    db = DatabaseConnection(str(tmp_path / "worldline.db"))
    _seed(db)
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
    ).fetchone()[0] == "hash-1"
    assert conn.execute("SELECT COUNT(*) FROM chapters WHERE novel_id = 'novel-1'").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM narrative_events WHERE novel_id = 'novel-1'").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM memory_atoms WHERE novel_id = 'novel-1'").fetchone()[0] == 1
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


def test_reset_past_the_current_tail_is_regular_continuation_not_destructive(tmp_path):
    db = DatabaseConnection(str(tmp_path / "worldline-continue.db"))
    _seed(db)
    service = WorldlineRegenerationService(db)

    preview = service.preview("novel-1", start_chapter=4, target_chapters=8)
    assert preview.operation == "continue"
    result = service.execute("novel-1", preview_token=preview.token, run_mode="continuous")

    assert result.operation == "continue"
    assert db.get_connection().execute("SELECT COUNT(*) FROM chapters WHERE novel_id = 'novel-1'").fetchone()[0] == 3


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
    assert [tuple(row) for row in rows] == [(1, "hash-1"), (2, "hash-2"), (3, "hash-3")]
    assert conn.execute(
        "SELECT status FROM worldline_archives WHERE id = ?", (archived.archive_id,)
    ).fetchone()[0] == "restored"
