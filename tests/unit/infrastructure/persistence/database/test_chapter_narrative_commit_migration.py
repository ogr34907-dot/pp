import hashlib
import sqlite3
from pathlib import Path

from domain.novel.entities.chapter import Chapter
from domain.novel.value_objects.novel_id import NovelId
from infrastructure.persistence.database.connection import DatabaseConnection
from infrastructure.persistence.database.sqlite_chapter_repository import (
    SqliteChapterRepository,
)


def _columns(db: DatabaseConnection, table: str) -> set[str]:
    return {row["name"] for row in db.fetch_all(f"PRAGMA table_info({table})")}


def test_clean_install_has_content_versions_summary_provenance_and_claim_table(tmp_path):
    db = DatabaseConnection(str(tmp_path / "clean.db"))

    assert {"content_sha256", "content_revision"} <= _columns(db, "chapters")
    assert {
        "source_content_sha256",
        "pipeline_version",
        "sync_status",
        "sync_error",
        "sync_attempts",
    } <= _columns(db, "chapter_summaries")
    assert {
        "novel_id",
        "chapter_number",
        "content_sha256",
        "pipeline_version",
        "content_revision",
        "status",
        "failure_reason",
        "attempt_count",
        "vector_status",
    } <= _columns(db, "chapter_narrative_commits")


def test_existing_chapter_and_summary_receive_mechanical_legacy_backfill(tmp_path):
    db_path = tmp_path / "legacy.db"
    schema = Path("infrastructure/persistence/database/schema.sql").read_text(encoding="utf-8")
    content = "旧章节正文"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(schema)
        conn.execute("INSERT INTO novels (id, title, slug) VALUES ('novel-1', 'Novel', 'novel-1')")
        conn.execute(
            "INSERT INTO chapters (id, novel_id, number, title, content) "
            "VALUES ('chapter-1', 'novel-1', 1, 'Chapter', ?)",
            (content,),
        )
        conn.execute(
            "INSERT INTO knowledge (id, novel_id) VALUES ('knowledge-1', 'novel-1')"
        )
        conn.execute(
            "INSERT INTO chapter_summaries (id, knowledge_id, chapter_number, summary) "
            "VALUES ('summary-1', 'knowledge-1', 1, '旧摘要')"
        )

    db = DatabaseConnection(str(db_path))
    expected_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
    chapter = db.fetch_one(
        "SELECT content_sha256, content_revision FROM chapters WHERE id = 'chapter-1'"
    )
    summary = db.fetch_one(
        "SELECT source_content_sha256, pipeline_version, sync_status, sync_attempts "
        "FROM chapter_summaries WHERE id = 'summary-1'"
    )

    assert dict(chapter) == {
        "content_sha256": expected_hash,
        "content_revision": 1,
    }
    assert dict(summary) == {
        "source_content_sha256": expected_hash,
        "pipeline_version": "legacy",
        "sync_status": "legacy",
        "sync_attempts": 0,
    }
    assert db.fetch_all("SELECT * FROM chapter_narrative_commits") == []


def test_chapter_repository_versions_only_changed_content(tmp_path):
    db = DatabaseConnection(str(tmp_path / "chapter-save.db"))
    db.execute("INSERT INTO novels (id, title, slug) VALUES ('novel-1', 'Novel', 'novel-1')")
    repo = SqliteChapterRepository(db)
    chapter = Chapter(
        id="chapter-1",
        novel_id=NovelId("novel-1"),
        number=1,
        title="Chapter",
        content="版本一",
    )

    repo.save(chapter)
    repo.save(chapter)
    first = db.fetch_one(
        "SELECT content_sha256, content_revision FROM chapters WHERE id = 'chapter-1'"
    )

    chapter.update_content("版本二")
    repo.save(chapter)
    second = db.fetch_one(
        "SELECT content_sha256, content_revision FROM chapters WHERE id = 'chapter-1'"
    )
    loaded = repo.get_by_novel_and_number(NovelId("novel-1"), 1)

    assert dict(first) == {
        "content_sha256": hashlib.sha256("版本一".encode("utf-8")).hexdigest(),
        "content_revision": 1,
    }
    assert dict(second) == {
        "content_sha256": hashlib.sha256("版本二".encode("utf-8")).hexdigest(),
        "content_revision": 2,
    }
    assert loaded.content_sha256 == second["content_sha256"]
    assert loaded.content_revision == 2
