"""Fixtures for API integration tests."""

import pytest
from fastapi.testclient import TestClient
import infrastructure.ai.prompt_manager as prompt_manager_module
import infrastructure.ai.prompt_registry as prompt_registry_module
from infrastructure.persistence.database.connection import DatabaseConnection
from infrastructure.persistence.database.sqlite_entity_base_repository import (
    SqliteEntityBaseRepository
)
from infrastructure.persistence.database.sqlite_narrative_event_repository import (
    SqliteNarrativeEventRepository
)

@pytest.fixture
def db(tmp_path):
    """File-backed SQLite fixture shared by the TestClient worker thread."""
    database = DatabaseConnection(str(tmp_path / "plotpilot.db"))
    yield database
    database.close_all(skip_checkpoint=True)


@pytest.fixture(autouse=True)
def _isolate_api_database(db, monkeypatch):
    """Route API and CPMS singleton dependencies to this test's SQLite database."""
    def get_test_database(*_args, **_kwargs):
        return db

    prompt_manager_module._manager_instance = None
    prompt_registry_module._registry_instance = None
    monkeypatch.setattr(
        "infrastructure.persistence.database.connection.get_database",
        get_test_database,
    )
    monkeypatch.setattr(
        "interfaces.api.dependencies.get_database",
        get_test_database,
    )
    monkeypatch.setattr(
        "interfaces.api.v1.engine.generation.get_database",
        get_test_database,
    )
    yield
    prompt_registry_module._registry_instance = None
    prompt_manager_module._manager_instance = None


@pytest.fixture
def client():
    """FastAPI test client using the autouse isolated database fixture."""
    from interfaces.main import app

    return TestClient(app)


@pytest.fixture
def test_novel_id(db):
    """Create a test novel and return its ID."""
    novel_id = "test-novel-1"
    db.execute(
        "INSERT INTO novels (id, title, slug, target_chapters) VALUES (?, ?, ?, ?)",
        (novel_id, "Test Novel", "test-novel", 10)
    )
    db.get_connection().commit()
    return novel_id


@pytest.fixture
def test_entity_id(db, test_novel_id):
    """Create a test entity and return its ID."""
    entity_id = "test-entity-1"
    core_attributes = {
        "name": "John Doe",
        "age": 30,
        "occupation": "Detective"
    }

    db.execute(
        "INSERT INTO entity_bases (id, novel_id, entity_type, core_attributes) VALUES (?, ?, ?, ?)",
        (entity_id, test_novel_id, "character", str(core_attributes))
    )
    db.get_connection().commit()
    return entity_id
