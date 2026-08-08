import asyncio
import hashlib
import threading

import pytest

from application.engine.services import query_service as query_service_module
from application.engine.services.query_service import QueryService
from application.engine.services.shared_state_repository import NovelState, SharedStateRepository
from interfaces.api.v1.engine import autopilot_routes
from infrastructure.persistence.database import connection as database_connection
from infrastructure.persistence.database.connection import DatabaseConnection


def _shared_status_with_stale_canonical_failure() -> SharedStateRepository:
    shared = SharedStateRepository(shared_dict={})
    shared.set_novel_state(
        "novel-1",
        NovelState(
            novel_id="novel-1",
            title="Demo",
            autopilot_status="stopped",
            current_stage="writing",
            current_act=1,
            current_chapter_in_act=8,
            current_beat_index=0,
            current_auto_chapters=8,
            target_chapters=100,
            target_words_per_chapter=2500,
            consecutive_error_count=0,
            last_chapter_tension=0,
            auto_approve_mode=False,
            needs_review=False,
            autopilot_pause_reason="canonical_aftermath_not_ready",
        ),
    )
    shared.merge_raw_state(
        "novel-1",
        canonical_aftermath_chapter_number=8,
        canonical_aftermath_failure_reason="stale shared failure",
    )
    return shared


def _seed_completed_chapter(database: DatabaseConnection, number: int, content: str) -> tuple[str, int]:
    content_sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()
    database.execute(
        "INSERT INTO chapters "
        "(id, novel_id, number, content, content_sha256, content_revision, status) "
        "VALUES (?, 'novel-1', ?, ?, ?, 1, 'completed')",
        (f"chapter-{number}", number, content, content_sha256),
    )
    return content_sha256, 1


def _seed_novel(database: DatabaseConnection) -> None:
    database.execute(
        "INSERT INTO novels (id, title, slug) VALUES ('novel-1', 'Demo', 'demo')"
    )


def test_status_prefers_latest_durable_terminal_failure_over_stale_shared_fields(
    tmp_path, monkeypatch
):
    """A status merger that keeps shared canonical fields must fail this test."""
    database = DatabaseConnection(str(tmp_path / "durable-failure.db"))
    _seed_novel(database)
    content_sha256, content_revision = _seed_completed_chapter(database, 63, "第六十三章正文")
    database.execute(
        "INSERT INTO chapter_narrative_commits "
        "(novel_id, chapter_number, content_sha256, pipeline_version, content_revision, "
        "status, failure_reason, attempt_count) "
        "VALUES ('novel-1', 63, ?, 'chapter-narrative-sync:v1', ?, "
        "'failed', 'durable provider failure', 3)",
        (content_sha256, content_revision),
    )
    database.commit()
    monkeypatch.setattr(database_connection, "get_database", lambda *_args, **_kwargs: database)

    status = QueryService(_shared_status_with_stale_canonical_failure()).get_novel_status_dict("novel-1")

    assert status is not None
    assert status["canonical_aftermath_chapter_number"] == 63
    assert status["canonical_aftermath_failure_reason"] == "durable provider failure"
    assert status["autopilot_pause_reason"] == "canonical_aftermath_not_ready"


def test_status_clears_stale_canonical_fields_when_latest_commit_is_ready(
    tmp_path, monkeypatch
):
    """A status merger that leaves a stale canonical pause must fail this test."""
    database = DatabaseConnection(str(tmp_path / "durable-ready.db"))
    _seed_novel(database)
    content_sha256, content_revision = _seed_completed_chapter(database, 63, "第六十三章正文")
    database.execute("INSERT INTO knowledge (id, novel_id) VALUES ('knowledge-1', 'novel-1')")
    database.execute(
        "INSERT INTO chapter_summaries "
        "(id, knowledge_id, chapter_number, summary, source_content_sha256, "
        "source_content_revision, pipeline_version, sync_status, sync_attempts) "
        "VALUES ('summary-63', 'knowledge-1', 63, '规范摘要', ?, ?, "
        "'chapter-narrative-sync:v1', 'committed', 1)",
        (content_sha256, content_revision),
    )
    database.execute(
        "INSERT INTO chapter_narrative_commits "
        "(novel_id, chapter_number, content_sha256, pipeline_version, content_revision, "
        "status, memory_status) VALUES ('novel-1', 63, ?, "
        "'chapter-narrative-sync:v1', ?, 'committed', 'committed')",
        (content_sha256, content_revision),
    )
    database.commit()
    monkeypatch.setattr(database_connection, "get_database", lambda *_args, **_kwargs: database)

    status = QueryService(_shared_status_with_stale_canonical_failure()).get_novel_status_dict("novel-1")

    assert status is not None
    assert "canonical_aftermath_chapter_number" not in status
    assert "canonical_aftermath_failure_reason" not in status
    assert status["autopilot_pause_reason"] == ""


def test_status_surfaces_exhausted_durable_memory_sync_failure(tmp_path, monkeypatch):
    """A resolver that only checks narrative failures must fail this test."""
    database = DatabaseConnection(str(tmp_path / "durable-memory-failure.db"))
    _seed_novel(database)
    content_sha256, content_revision = _seed_completed_chapter(database, 63, "第六十三章正文")
    database.execute(
        "INSERT INTO chapter_narrative_commits "
        "(novel_id, chapter_number, content_sha256, pipeline_version, content_revision, "
        "status, memory_status, memory_failure_reason, memory_attempt_count) "
        "VALUES ('novel-1', 63, ?, 'chapter-narrative-sync:v1', ?, "
        "'committed', 'failed', 'durable memory failure', 3)",
        (content_sha256, content_revision),
    )
    database.commit()
    monkeypatch.setattr(database_connection, "get_database", lambda *_args, **_kwargs: database)

    status = QueryService(_shared_status_with_stale_canonical_failure()).get_novel_status_dict("novel-1")

    assert status is not None
    assert status["canonical_aftermath_chapter_number"] == 63
    assert status["canonical_aftermath_failure_reason"] == "durable memory failure"


def test_status_preserves_gate_when_latest_commit_is_missing(tmp_path, monkeypatch):
    database = DatabaseConnection(str(tmp_path / "durable-missing.db"))
    _seed_novel(database)
    _seed_completed_chapter(database, 63, "第六十三章正文")
    database.commit()
    monkeypatch.setattr(database_connection, "get_database", lambda *_args, **_kwargs: database)

    status = QueryService(_shared_status_with_stale_canonical_failure()).get_novel_status_dict("novel-1")

    assert status is not None
    assert status["canonical_aftermath_chapter_number"] == 63
    assert status["canonical_aftermath_failure_reason"] == "canonical_aftermath_not_ready"
    assert status["autopilot_pause_reason"] == "canonical_aftermath_not_ready"


def test_status_preserves_gate_while_memory_sync_is_pending(tmp_path, monkeypatch):
    database = DatabaseConnection(str(tmp_path / "durable-memory-pending.db"))
    _seed_novel(database)
    content_sha256, content_revision = _seed_completed_chapter(database, 63, "第六十三章正文")
    database.execute(
        "INSERT INTO chapter_narrative_commits "
        "(novel_id, chapter_number, content_sha256, pipeline_version, content_revision, "
        "status, memory_status) VALUES ('novel-1', 63, ?, "
        "'chapter-narrative-sync:v1', ?, 'committed', 'pending')",
        (content_sha256, content_revision),
    )
    database.commit()
    monkeypatch.setattr(database_connection, "get_database", lambda *_args, **_kwargs: database)

    status = QueryService(_shared_status_with_stale_canonical_failure()).get_novel_status_dict("novel-1")

    assert status is not None
    assert status["canonical_aftermath_chapter_number"] == 63
    assert status["canonical_aftermath_failure_reason"] == "canonical_aftermath_not_ready"
    assert status["autopilot_pause_reason"] == "canonical_aftermath_not_ready"


@pytest.mark.asyncio
async def test_status_durable_reconciliation_does_not_block_event_loop(monkeypatch):
    entered = threading.Event()
    release = threading.Event()

    class _BlockingQuery:
        def get_novel_status_dict(self, novel_id: str):
            entered.set()
            release.wait(timeout=1.0)
            return {"novel_id": novel_id}

    monkeypatch.setattr(
        query_service_module,
        "get_query_service",
        lambda: _BlockingQuery(),
    )
    timer = threading.Timer(0.5, release.set)
    timer.start()
    loop = asyncio.get_running_loop()
    started_at = loop.time()
    task = asyncio.create_task(autopilot_routes.get_autopilot_status("novel-1"))
    try:
        await asyncio.sleep(0.02)
        elapsed = loop.time() - started_at
        assert entered.is_set()
        assert elapsed < 0.2
    finally:
        release.set()
        timer.cancel()
    assert await task == {"novel_id": "novel-1"}
