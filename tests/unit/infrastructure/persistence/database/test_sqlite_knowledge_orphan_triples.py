"""无 knowledge 表行时仍能按 triples 读出事实（Bible 同步等场景）。"""
import sqlite3
from pathlib import Path

import pytest

from domain.novel.value_objects.novel_id import NovelId
from infrastructure.persistence.database.connection import DatabaseConnection
from infrastructure.persistence.database.sqlite_knowledge_repository import SqliteKnowledgeRepository

SCHEMA_PATH = (
    Path(__file__).resolve().parents[5] / "infrastructure" / "persistence" / "database" / "schema.sql"
)


@pytest.fixture
def repo(tmp_path):
    db_path = tmp_path / "t.db"
    conn = sqlite3.connect(str(db_path))
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    conn.execute(
        "INSERT INTO novels (id, title, slug, target_chapters) VALUES ('n1', 'T', 'slug1', 0)"
    )
    conn.execute(
        """
        INSERT INTO triples (
            id, novel_id, subject, predicate, object, chapter_number, note,
            entity_type, importance, location_type, description, first_appearance,
            confidence, source_type, subject_entity_id, object_entity_id
        ) VALUES (
            't-loc-1', 'n1', '青城', '地图地点', '青城', NULL, '',
            'location', 'normal', 'region', NULL, NULL,
            1.0, 'bible_generated', 'loc1', 'loc1'
        )
        """
    )
    conn.commit()
    conn.close()
    db = DatabaseConnection(str(db_path))
    return SqliteKnowledgeRepository(db)


def test_get_by_novel_id_returns_facts_without_knowledge_row(repo):
    sk = repo.get_by_novel_id(NovelId("n1"))
    assert sk is not None
    assert sk.premise_lock == ""
    assert sk.chapters == []
    assert len(sk.facts) == 1
    assert sk.facts[0].subject == "青城"
    assert sk.facts[0].entity_type == "location"
    assert sk.facts[0].source_type == "bible_generated"


def _insert_chapter(repo, chapter_number: int) -> None:
    repo.db.execute(
        """
        INSERT INTO chapters (
            id, novel_id, number, title, content, content_sha256, content_revision
        ) VALUES (?, 'n1', ?, ?, '正文', ?, 1)
        """,
        (
            f"chapter-{chapter_number}",
            chapter_number,
            f"第{chapter_number}章",
            f"hash-{chapter_number}",
        ),
    )


def _insert_triple(repo, triple_id: str, source_type: str | None) -> None:
    repo.db.execute(
        """
        INSERT INTO triples (
            id, novel_id, subject, predicate, object, chapter_number,
            confidence, source_type
        ) VALUES (?, 'n1', '甲', '状态', ?, 1, 1.0, ?)
        """,
        (triple_id, triple_id, source_type),
    )


def _insert_commit(repo, status: str) -> None:
    repo.db.execute(
        """
        INSERT INTO chapter_narrative_commits (
            novel_id, chapter_number, content_sha256, pipeline_version,
            content_revision, status
        ) VALUES ('n1', 1, 'hash-1', 'chapter-narrative-sync:v1', 1, ?)
        """,
        (status,),
    )


def _fact_ids(repo) -> set[str]:
    knowledge = repo.get_by_novel_id(NovelId("n1"))
    return {fact.id for fact in (knowledge.facts if knowledge else [])}


def test_autopilot_extract_without_commit_is_not_visible(repo):
    _insert_chapter(repo, 1)
    _insert_triple(repo, "autopilot-no-commit", "autopilot_extract")
    repo.db.commit()

    assert "autopilot-no-commit" not in _fact_ids(repo)


def test_autopilot_extract_with_failed_commit_is_not_visible(repo):
    _insert_chapter(repo, 1)
    _insert_triple(repo, "autopilot-failed", "autopilot_extract")
    _insert_commit(repo, "failed")
    repo.db.commit()

    assert "autopilot-failed" not in _fact_ids(repo)


def test_autopilot_extract_with_committed_chapter_is_visible(repo):
    _insert_chapter(repo, 1)
    _insert_triple(repo, "autopilot-committed", "autopilot_extract")
    _insert_commit(repo, "committed")
    repo.db.commit()

    assert "autopilot-committed" in _fact_ids(repo)


def test_static_and_compatibility_sources_remain_visible_without_commit(repo):
    _insert_chapter(repo, 1)
    _insert_triple(repo, "static-null", None)
    _insert_triple(repo, "static-empty", "")
    _insert_triple(repo, "manual-source", "manual")
    _insert_triple(repo, "chapter-inferred-source", "chapter_inferred")
    repo.db.commit()

    assert {
        "static-null",
        "static-empty",
        "manual-source",
        "chapter-inferred-source",
    } <= _fact_ids(repo)
