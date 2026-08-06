import pytest

from application.engine.services.query_service import QueryService
from application.engine.services.shared_state_repository import SharedStateRepository
from application.engine.services.state_bootstrap import StateBootstrap
from infrastructure.persistence.database.connection import DatabaseConnection


@pytest.fixture
def knowledge_database(tmp_path):
    db = DatabaseConnection(str(tmp_path / "knowledge.db"))
    conn = db.get_connection()
    conn.execute(
        "INSERT INTO novels (id, title, slug) VALUES (?, ?, ?)",
        ("novel-1", "Test novel", "test-novel"),
    )
    conn.execute(
        """
        INSERT INTO knowledge (id, novel_id, version, premise_lock)
        VALUES (?, ?, ?, ?)
        """,
        ("novel-1-knowledge", "novel-1", 7, "A locked premise"),
    )
    conn.execute(
        """
        INSERT INTO chapters (id, novel_id, number, title, content, status)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        ("chapter-1", "novel-1", 1, "Chapter one", "Body", "completed"),
    )
    conn.execute(
        """
        INSERT INTO chapter_summaries (
            id, knowledge_id, chapter_number, summary, key_events,
            open_threads, consistency_note, beat_sections, micro_beats,
            sync_status
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "novel-1-knowledge-ch1",
            "novel-1-knowledge",
            1,
            "Chapter summary",
            "An event",
            "An open thread",
            "No contradiction",
            '["beat one"]',
            "[]",
            "synced",
        ),
    )
    conn.execute(
        """
        INSERT INTO triples (
            id, novel_id, subject, predicate, object, chapter_number, note
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        ("fact-1", "novel-1", "Hero", "owns", "Key", 1, "Important"),
    )
    conn.commit()

    yield db

    db.close_all(skip_checkpoint=True)


@pytest.fixture
def expected_knowledge():
    return {
        "version": 7,
        "premise_lock": "A locked premise",
        "chapters": [
            {
                "chapter_id": 1,
                "summary": "Chapter summary",
                "key_events": "An event",
                "open_threads": "An open thread",
                "consistency_note": "No contradiction",
                "beat_sections": ["beat one"],
                "sync_status": "synced",
            }
        ],
        "facts": [
            {
                "id": "fact-1",
                "subject": "Hero",
                "predicate": "owns",
                "object": "Key",
                "chapter_id": 1,
                "note": "Important",
            }
        ],
    }


def test_load_knowledge_serializes_and_caches_existing_sqlite_knowledge(
    monkeypatch, knowledge_database, expected_knowledge
):
    """STATE-BOOTSTRAP-001: persisted knowledge must be available after preloading."""
    monkeypatch.setattr(
        "infrastructure.persistence.database.connection.get_database",
        lambda: knowledge_database,
    )
    shared = SharedStateRepository(shared_dict={})

    loaded = StateBootstrap(shared_state=shared)._load_knowledge("novel-1")

    assert loaded == expected_knowledge
    assert shared.get_knowledge("novel-1") == expected_knowledge


def test_load_novel_preloads_knowledge_for_memory_first_workbench(
    monkeypatch, knowledge_database, expected_knowledge
):
    """STATE-BOOTSTRAP-002: normal single-novel bootstrap must hydrate knowledge."""
    monkeypatch.setattr(
        "infrastructure.persistence.database.connection.get_database",
        lambda: knowledge_database,
    )
    shared = SharedStateRepository(shared_dict={})

    assert StateBootstrap(shared_state=shared).load_novel("novel-1") is True

    assert shared.get_knowledge("novel-1") == expected_knowledge


def test_load_all_preloads_knowledge_for_every_novel(
    monkeypatch, knowledge_database, expected_knowledge
):
    """STATE-BOOTSTRAP-003: full startup bootstrap must hydrate each novel's knowledge."""
    monkeypatch.setattr(
        "infrastructure.persistence.database.connection.get_database",
        lambda: knowledge_database,
    )
    shared = SharedStateRepository(shared_dict={})

    stats = StateBootstrap(shared_state=shared).load_all()

    assert stats["novels_loaded"] == 1
    assert shared.get_knowledge("novel-1") == expected_knowledge


def test_workbench_db_fallback_returns_existing_knowledge(
    monkeypatch, knowledge_database, expected_knowledge
):
    """WORKBENCH-001: a cold shared cache must not discard persisted knowledge."""
    monkeypatch.setattr(
        "infrastructure.persistence.database.connection.get_database",
        lambda: knowledge_database,
    )

    response = QueryService(
        SharedStateRepository(shared_dict={})
    ).get_workbench_context("novel-1")

    assert response._from_shared_memory is False
    assert response.knowledge == expected_knowledge
