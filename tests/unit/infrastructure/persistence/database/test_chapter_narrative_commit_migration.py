import hashlib
import sqlite3
from pathlib import Path

from domain.novel.entities.chapter import Chapter
from domain.novel.value_objects.novel_id import NovelId
from infrastructure.persistence.database.connection import (
    DatabaseConnection,
    _apply_chapter_narrative_commit_migration,
    _apply_migration_files,
)
from infrastructure.persistence.database.sqlite_chapter_repository import (
    SqliteChapterRepository,
)
from infrastructure.persistence.database.sqlite_chapter_narrative_commit_repository import (
    SqliteChapterNarrativeCommitRepository,
)
from infrastructure.persistence.database.sqlite_novel_repository import (
    SqliteNovelRepository,
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
        "canonical_payload_sha256",
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
        "advance_status",
        "advance_applied_at",
    } <= _columns(db, "chapter_narrative_commits")
    narrative_default = next(
        row["dflt_value"]
        for row in db.fetch_all("PRAGMA table_info(novels)")
        if row["name"] == "last_audit_narrative_ok"
    )
    assert narrative_default == "0"


def test_novel_hydration_treats_unknown_narrative_audit_as_failed(tmp_path):
    db = DatabaseConnection(str(tmp_path / "unknown-audit.db"))
    db.execute(
        "INSERT INTO novels "
        "(id, title, slug, last_audit_narrative_ok) "
        "VALUES ('novel-1', 'Novel', 'novel-1', NULL)"
    )

    novel = SqliteNovelRepository(db).get_by_id(NovelId("novel-1"))

    assert novel is not None
    assert novel.last_audit_narrative_ok is False


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
        "SELECT source_content_sha256, pipeline_version, sync_status, sync_attempts, "
        "canonical_payload_sha256 "
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
        "canonical_payload_sha256": "",
    }
    assert db.fetch_all("SELECT * FROM chapter_narrative_commits") == []


def test_migration_skips_hash_backfill_for_legacy_chapters_without_content(tmp_path):
    db_path = tmp_path / "legacy-without-content.db"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE chapters (
                id TEXT PRIMARY KEY,
                novel_id TEXT NOT NULL,
                number INTEGER NOT NULL
            );
            CREATE TABLE knowledge (
                id TEXT PRIMARY KEY,
                novel_id TEXT NOT NULL
            );
            CREATE TABLE chapter_summaries (
                id TEXT PRIMARY KEY,
                knowledge_id TEXT NOT NULL,
                chapter_number INTEGER NOT NULL,
                summary TEXT,
                sync_status TEXT NOT NULL DEFAULT 'draft'
            );
            """
        )

        _apply_chapter_narrative_commit_migration(conn)

        columns = {row[1] for row in conn.execute("PRAGMA table_info(chapters)")}

    assert {"content_sha256", "content_revision"} <= columns


def test_story_pipeline_advance_is_applied_exactly_once_per_committed_revision(tmp_path):
    db = DatabaseConnection(str(tmp_path / "advance-once.db"))
    content = "已规范提交的正文"
    content_sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()
    db.execute(
        "INSERT INTO novels (id, title, slug, current_auto_chapters, "
        "current_chapter_in_act, current_beat_index, beats_completed, current_stage) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        ("novel-1", "Novel", "novel-1", 0, 0, 3, 1, "writing"),
    )
    db.execute(
        "INSERT INTO chapters (id, novel_id, number, title, content, status, "
        "content_sha256, content_revision) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "chapter-1",
            "novel-1",
            1,
            "Chapter",
            content,
            "completed",
            content_sha256,
            1,
        ),
    )
    db.execute(
        "INSERT INTO chapter_narrative_commits "
        "(novel_id, chapter_number, content_sha256, pipeline_version, "
        "content_revision, status, advance_status) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            "novel-1",
            1,
            content_sha256,
            "chapter-narrative-sync:v1",
            1,
            "committed",
            "pending",
        ),
    )
    db.get_connection().commit()

    repository = SqliteChapterNarrativeCommitRepository(db)
    first = repository.advance_story_pipeline_once(
        novel_id="novel-1",
        chapter_number=1,
        pipeline_version="chapter-narrative-sync:v1",
    )
    second = repository.advance_story_pipeline_once(
        novel_id="novel-1",
        chapter_number=1,
        pipeline_version="chapter-narrative-sync:v1",
    )

    assert first.disposition == "applied"
    assert second.disposition == "already_applied"
    assert (first.current_auto_chapters, first.current_chapter_in_act) == (1, 1)
    row = db.fetch_one(
        "SELECT current_auto_chapters, current_chapter_in_act, current_beat_index, "
        "beats_completed, current_stage "
        "FROM novels WHERE id = ?",
        ("novel-1",),
    )
    assert dict(row) == {
        "current_auto_chapters": 1,
        "current_chapter_in_act": 1,
        "current_beat_index": 0,
        "beats_completed": 0,
        "current_stage": "auditing",
    }
    commit = db.fetch_one(
        "SELECT advance_status, advance_applied_at FROM chapter_narrative_commits"
    )
    assert commit["advance_status"] == "applied"
    assert commit["advance_applied_at"]


def test_pending_story_pipeline_advance_is_recovered_without_a_second_increment(tmp_path):
    db = DatabaseConnection(str(tmp_path / "advance-recovery.db"))
    content = "崩溃前已提交的正文"
    content_sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()
    db.execute(
        "INSERT INTO novels (id, title, slug, current_auto_chapters, "
        "current_chapter_in_act, current_stage) VALUES (?, ?, ?, ?, ?, ?)",
        ("novel-1", "Novel", "novel-1", 0, 0, "writing"),
    )
    db.execute(
        "INSERT INTO chapters (id, novel_id, number, title, content, status, "
        "content_sha256, content_revision) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "chapter-1",
            "novel-1",
            1,
            "Chapter",
            content,
            "completed",
            content_sha256,
            1,
        ),
    )
    db.execute(
        "INSERT INTO chapter_narrative_commits "
        "(novel_id, chapter_number, content_sha256, pipeline_version, "
        "content_revision, status, advance_status) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            "novel-1",
            1,
            content_sha256,
            "chapter-narrative-sync:v1",
            1,
            "committed",
            "pending",
        ),
    )
    db.get_connection().commit()

    repository = SqliteChapterNarrativeCommitRepository(db)
    recovered = repository.recover_pending_story_pipeline_advances(
        novel_id="novel-1",
        pipeline_version="chapter-narrative-sync:v1",
    )
    second_recovery = repository.recover_pending_story_pipeline_advances(
        novel_id="novel-1",
        pipeline_version="chapter-narrative-sync:v1",
    )

    assert [advance.disposition for advance in recovered] == ["applied"]
    assert second_recovery == []
    row = db.fetch_one(
        "SELECT current_auto_chapters, current_chapter_in_act FROM novels WHERE id = ?",
        ("novel-1",),
    )
    assert dict(row) == {
        "current_auto_chapters": 1,
        "current_chapter_in_act": 1,
    }


def test_story_pipeline_advance_rejects_a_canonical_record_for_a_stale_revision(tmp_path):
    db = DatabaseConnection(str(tmp_path / "advance-stale.db"))
    old_content = "旧正文"
    old_sha256 = hashlib.sha256(old_content.encode("utf-8")).hexdigest()
    new_content = "新正文"
    new_sha256 = hashlib.sha256(new_content.encode("utf-8")).hexdigest()
    db.execute(
        "INSERT INTO novels (id, title, slug, current_auto_chapters, "
        "current_chapter_in_act, current_stage) VALUES (?, ?, ?, ?, ?, ?)",
        ("novel-1", "Novel", "novel-1", 0, 0, "writing"),
    )
    db.execute(
        "INSERT INTO chapters (id, novel_id, number, title, content, status, "
        "content_sha256, content_revision) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "chapter-1",
            "novel-1",
            1,
            "Chapter",
            new_content,
            "completed",
            new_sha256,
            2,
        ),
    )
    db.execute(
        "INSERT INTO chapter_narrative_commits "
        "(novel_id, chapter_number, content_sha256, pipeline_version, "
        "content_revision, status, advance_status) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (
            "novel-1",
            1,
            old_sha256,
            "chapter-narrative-sync:v1",
            1,
            "committed",
            "pending",
        ),
    )
    db.get_connection().commit()

    result = SqliteChapterNarrativeCommitRepository(db).advance_story_pipeline_once(
        novel_id="novel-1",
        chapter_number=1,
        pipeline_version="chapter-narrative-sync:v1",
    )

    assert result.disposition == "source_version_mismatch"
    row = db.fetch_one(
        "SELECT current_auto_chapters, current_chapter_in_act FROM novels WHERE id = ?",
        ("novel-1",),
    )
    assert dict(row) == {
        "current_auto_chapters": 0,
        "current_chapter_in_act": 0,
    }


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


def test_migration_runner_upgrades_existing_database_with_canonical_provenance(
    tmp_path,
):
    db_path = tmp_path / "runner-legacy.db"
    migrations_dir = Path("infrastructure/persistence/database/migrations")
    content = "迁移脚本下的旧正文"
    with sqlite3.connect(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE chapters (
                id TEXT PRIMARY KEY,
                novel_id TEXT NOT NULL,
                number INTEGER NOT NULL,
                content TEXT DEFAULT ''
            );
            CREATE TABLE knowledge (
                id TEXT PRIMARY KEY,
                novel_id TEXT NOT NULL
            );
            CREATE TABLE chapter_summaries (
                id TEXT PRIMARY KEY,
                knowledge_id TEXT NOT NULL,
                chapter_number INTEGER NOT NULL,
                summary TEXT DEFAULT '',
                sync_status TEXT DEFAULT 'draft'
            );
            CREATE TABLE chapter_narrative_commits (
                novel_id TEXT NOT NULL,
                chapter_number INTEGER NOT NULL,
                content_sha256 TEXT NOT NULL,
                pipeline_version TEXT NOT NULL,
                content_revision INTEGER NOT NULL,
                status TEXT NOT NULL DEFAULT 'in_progress',
                failure_reason TEXT NOT NULL DEFAULT '',
                attempt_count INTEGER NOT NULL DEFAULT 1,
                vector_status TEXT NOT NULL DEFAULT 'not_started',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                committed_at TIMESTAMP,
                PRIMARY KEY (novel_id, chapter_number, content_sha256, pipeline_version)
            );
            CREATE TABLE migrations_applied (
                migration_file TEXT PRIMARY KEY,
                applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            );
            """
        )
        conn.execute(
            "INSERT INTO chapters (id, novel_id, number, content) VALUES (?, ?, ?, ?)",
            ("chapter-1", "novel-1", 1, content),
        )
        conn.execute("INSERT INTO knowledge (id, novel_id) VALUES ('knowledge-1', 'novel-1')")
        conn.execute(
            "INSERT INTO chapter_summaries (id, knowledge_id, chapter_number, summary) "
            "VALUES ('summary-1', 'knowledge-1', 1, '旧摘要')"
        )
        conn.execute(
            "INSERT INTO chapter_narrative_commits "
            "(novel_id, chapter_number, content_sha256, pipeline_version, "
            "content_revision, status) VALUES (?, ?, ?, ?, ?, ?)",
            ("novel-1", 1, "legacy-hash", "chapter-narrative-sync:v1", 1, "committed"),
        )
        for migration in migrations_dir.glob("*.sql"):
            if migration.name not in {
                "014_chapter_narrative_commits.sql",
                "015_chapter_summary_payload_digest.sql",
                "017_story_pipeline_advance.sql",
            }:
                conn.execute(
                    "INSERT INTO migrations_applied (migration_file) VALUES (?)",
                    (migration.name,),
                )
        _apply_migration_files(conn)

        chapter_columns = {row[1] for row in conn.execute("PRAGMA table_info(chapters)")}
        summary_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(chapter_summaries)")
        }
        chapter = conn.execute(
            "SELECT content_sha256, content_revision FROM chapters WHERE id = 'chapter-1'"
        ).fetchone()
        summary = conn.execute(
            "SELECT source_content_sha256, pipeline_version, sync_status, sync_attempts, "
            "canonical_payload_sha256 "
            "FROM chapter_summaries WHERE id = 'summary-1'"
        ).fetchone()
        commit_columns = {
            row[1] for row in conn.execute("PRAGMA table_info(chapter_narrative_commits)")
        }
        legacy_commit = conn.execute(
            "SELECT advance_status, advance_applied_at "
            "FROM chapter_narrative_commits WHERE novel_id = 'novel-1'"
        ).fetchone()

    assert {"content_sha256", "content_revision"} <= chapter_columns
    assert {
        "source_content_sha256",
        "pipeline_version",
        "sync_error",
        "sync_attempts",
        "canonical_payload_sha256",
    } <= summary_columns
    assert chapter == (hashlib.sha256(content.encode("utf-8")).hexdigest(), 1)
    assert summary == (chapter[0], "legacy", "legacy", 0, "")
    assert {"advance_status", "advance_applied_at"} <= commit_columns
    assert legacy_commit[0] == "applied"
    assert legacy_commit[1]
