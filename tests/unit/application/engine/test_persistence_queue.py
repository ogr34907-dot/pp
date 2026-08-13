import hashlib
import sqlite3

import pytest

from application.core.config.config_loader import reload_config
from application.engine.services import persistence_queue as persistence_queue_module
from application.engine.services.persistence_queue import (
    PersistenceCommandType,
    PersistenceQueue,
    register_persistence_handlers,
)
from infrastructure.persistence.database.connection import DatabaseConnection


def test_legacy_persistence_queue_uses_configured_lock_backoff(tmp_path, monkeypatch):
    config_path = tmp_path / "performance.yaml"
    config_path.write_text(
        """
persistence_queue:
  legacy_lock_max_retries: 3
  legacy_lock_backoff_base_seconds: 0.1
  legacy_lock_backoff_max_seconds: 0.15
""",
        encoding="utf-8",
    )

    sleeps = []
    attempts = {"count": 0}

    def sleep(seconds):
        sleeps.append(seconds)

    def handler(_payload):
        attempts["count"] += 1
        if attempts["count"] < 3:
            raise sqlite3.OperationalError("database is locked")

    try:
        reload_config(str(config_path))
        monkeypatch.setattr("application.engine.services.persistence_queue.time.sleep", sleep)

        queue = PersistenceQueue()
        queue.register_handler("cmd", handler)
        queue._process_single_command("cmd", {})

        assert attempts["count"] == 3
        assert sleeps == pytest.approx([0.1, 0.15])
    finally:
        reload_config()


def test_upsert_chapter_handler_commits_hash_and_revision(tmp_path, monkeypatch):
    db = DatabaseConnection(str(tmp_path / "queue.db"))
    db.execute(
        "INSERT INTO novels (id, title, slug) VALUES ('novel-1', 'Novel', 'novel-1')"
    )
    queue = PersistenceQueue()
    monkeypatch.setattr(persistence_queue_module, "_persistence_queue", queue)
    monkeypatch.setattr(
        "infrastructure.persistence.database.connection.get_database",
        lambda *args, **kwargs: db,
    )
    register_persistence_handlers()

    queue._handlers[PersistenceCommandType.UPSERT_CHAPTER.value](
        {
            "chapter_id": "chapter-1",
            "novel_id": "novel-1",
            "chapter_number": 1,
            "content": "队列正文",
            "status": "completed",
            "word_count": 4,
        }
    )

    row = db.fetch_one(
        "SELECT content_sha256, content_revision FROM chapters "
        "WHERE novel_id = 'novel-1' AND number = 1"
    )
    assert dict(row) == {
        "content_sha256": hashlib.sha256("队列正文".encode("utf-8")).hexdigest(),
        "content_revision": 1,
    }


def test_upsert_chapter_handler_allows_uncommitted_draft_update(tmp_path, monkeypatch):
    db = DatabaseConnection(str(tmp_path / "queue-draft.db"))
    db.execute(
        "INSERT INTO novels (id, title, slug) VALUES ('novel-1', 'Novel', 'novel-1')"
    )
    queue = PersistenceQueue()
    monkeypatch.setattr(persistence_queue_module, "_persistence_queue", queue)
    monkeypatch.setattr(
        "infrastructure.persistence.database.connection.get_database",
        lambda *args, **kwargs: db,
    )
    register_persistence_handlers()
    handler = queue._handlers[PersistenceCommandType.UPSERT_CHAPTER.value]

    for content in ("首段草稿", "首段草稿\n\n追加草稿"):
        handler(
            {
                "chapter_id": "chapter-1",
                "novel_id": "novel-1",
                "chapter_number": 1,
                "content": content,
                "status": "draft",
            }
        )

    row = db.fetch_one(
        "SELECT content, content_revision FROM chapters "
        "WHERE novel_id = 'novel-1' AND number = 1"
    )
    assert dict(row) == {"content": "首段草稿\n\n追加草稿", "content_revision": 2}


def test_upsert_chapter_handler_rejects_formal_overwrite(tmp_path, monkeypatch):
    db = DatabaseConnection(str(tmp_path / "queue-guard.db"))
    db.execute(
        "INSERT INTO novels (id, title, slug) VALUES ('novel-1', 'Novel', 'novel-1')"
    )
    queue = PersistenceQueue()
    monkeypatch.setattr(persistence_queue_module, "_persistence_queue", queue)
    monkeypatch.setattr(
        "infrastructure.persistence.database.connection.get_database",
        lambda *args, **kwargs: db,
    )
    register_persistence_handlers()
    handler = queue._handlers[PersistenceCommandType.UPSERT_CHAPTER.value]
    handler(
        {
            "chapter_id": "chapter-1",
            "novel_id": "novel-1",
            "chapter_number": 1,
            "content": "正式正文",
            "status": "completed",
        }
    )

    with pytest.raises(RuntimeError, match="ChapterRewriteCoordinator"):
        handler(
            {
                "chapter_id": "chapter-1",
                "novel_id": "novel-1",
                "chapter_number": 1,
                "content": "旁路覆盖",
                "status": "completed",
            }
        )


def test_upsert_chapter_handler_propagates_non_lock_failure(monkeypatch):
    class _FailingDatabase:
        def fetch_one(self, *args, **kwargs):
            return None

        def execute(self, *args, **kwargs):
            raise RuntimeError("disk unavailable")

    queue = PersistenceQueue()
    monkeypatch.setattr(persistence_queue_module, "_persistence_queue", queue)
    monkeypatch.setattr(
        "infrastructure.persistence.database.connection.get_database",
        lambda *args, **kwargs: _FailingDatabase(),
    )
    register_persistence_handlers()

    with pytest.raises(RuntimeError, match="disk unavailable"):
        queue._handlers[PersistenceCommandType.UPSERT_CHAPTER.value](
            {
                "novel_id": "novel-1",
                "chapter_number": 1,
                "content": "正文",
                "status": "completed",
            }
        )
