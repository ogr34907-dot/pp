"""Manifest authority closes legacy outline and StoryNode HTTP writers."""

from __future__ import annotations

from typing import Any
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from application.blueprint.services.outline_contract_service import OutlineContractService
from domain.structure.outline_contract import OutlinePayload
from domain.structure.outline_plan import OutlinePlanItem
from infrastructure.persistence.database.outline_contract_repository import (
    OutlineContractRepository,
)
from infrastructure.persistence.database.planning_authority_guard import (
    PlanningAuthorityError,
)
from infrastructure.persistence.database.story_node_repository import StoryNodeRepository
from interfaces.api.v1.blueprint import outline_routes
from interfaces.api.v1.blueprint import story_structure as story_structure_routes


MANIFEST_DETAIL = "manifest_planning_authority"


def _mark_manifest(db, novel_id: str) -> str:
    contracts = OutlineContractRepository(db)
    root = contracts.ensure_root(novel_id)
    drafted = contracts.save_draft(root.id, OutlinePayload(title="Manifest root"))
    published = contracts.publish_and_sync(
        root.id,
        expected_revision=drafted.draft.revision,
        idempotency_key="manifest-root",
    )
    active = db.get_connection().execute(
        """
        SELECT contract.active_version_id AS version_id, version.digest
        FROM outline_contracts AS contract
        JOIN outline_contract_versions AS version
          ON version.id = contract.active_version_id
        WHERE contract.id = ?
        """,
        (published.id,),
    ).fetchone()
    item = OutlinePlanItem(
        logical_node_id=published.id,
        version_id=str(active["version_id"]),
        version_digest=str(active["digest"]),
        level=published.level,
        sibling_index=0,
    )
    plan = contracts.create_plan_draft(
        novel_id=novel_id,
        items=(item,),
        canonical_prefix_digest="",
        canonical_boundary={"formal_head": 0},
    )
    plan = contracts.seal_plan_revision(plan.id)
    db.get_connection().execute(
        """
        UPDATE outline_planning_heads
        SET authority_mode = 'manifest', authority_generation = 1,
            projection_generation = 1, active_plan_revision_id = ?,
            active_plan_digest = ?
        WHERE novel_id = ?
        """,
        (plan.id, plan.digest, novel_id),
    )
    db.get_connection().commit()
    row = db.get_connection().execute(
        "SELECT authority_mode FROM outline_planning_heads WHERE novel_id = ?",
        (novel_id,),
    ).fetchone()
    assert row["authority_mode"] == "manifest"
    return root.id


def _request(client: TestClient, method: str, path: str, body: dict | None):
    return client.request(method.upper(), path, json=body)


@pytest.fixture
def http_client(db):
    from interfaces.main import app

    service = OutlineContractService(
        contract_repository=OutlineContractRepository(db),
        story_node_repository=StoryNodeRepository(db),
    )
    app.dependency_overrides[outline_routes.get_outline_service] = lambda: service
    client = TestClient(app, raise_server_exceptions=False)
    try:
        yield client
    finally:
        client.close()
        app.dependency_overrides.pop(outline_routes.get_outline_service, None)


class StubStructureService:
    def __init__(self, db) -> None:
        self.repository = SimpleNamespace(_get_connection=db.get_connection)
        self.failure: Exception | None = None
        self.calls: list[str] = []

    def _write(self, operation: str, result: Any) -> Any:
        self.calls.append(operation)
        if self.failure is not None:
            raise self.failure
        return result

    async def get_tree(self, novel_id: str):
        return {"novel_id": novel_id, "tree": {"nodes": []}}

    async def get_children(self, novel_id: str, parent_id: str | None = None):
        return []

    async def create_node(self, **_kwargs):
        return self._write("create", {"id": "node-created"})

    async def update_node(self, **_kwargs):
        return self._write("update", {"id": "node-1"})

    async def delete_node(self, _node_id: str):
        return self._write("delete", True)

    async def reorder_nodes(self, _node_ids: list[str]):
        return self._write("reorder", [])

    async def update_chapter_ranges(self, _novel_id: str):
        return self._write("update-ranges", None)

    async def create_default_structure(self, **_kwargs):
        return self._write("create-default", [])


@pytest.fixture
def structure_http_client(db, monkeypatch):
    from interfaces.main import app

    service = StubStructureService(db)
    monkeypatch.setattr(story_structure_routes, "get_database", lambda: db)
    app.dependency_overrides[story_structure_routes.get_service] = lambda: service
    client = TestClient(app, raise_server_exceptions=False)
    try:
        yield client, service
    finally:
        client.close()
        app.dependency_overrides.pop(story_structure_routes.get_service, None)


OUTLINE_MUTATIONS = (
    ("post", "/api/v1/outline/contracts/{root_id}/draft", {"payload": {"title": "blocked"}}),
    ("post", "/api/v1/outline/contracts/{root_id}/publish", {"expected_revision": 1}),
    ("post", "/api/v1/outline/novels/{novel_id}/story-nodes/node-1/contract", None),
)


@pytest.mark.parametrize(("method", "path_template", "body"), OUTLINE_MUTATIONS)
def test_manifest_authority_returns_stable_gone_for_legacy_outline_mutations(
    http_client, db, test_novel_id, method: str, path_template: str, body: dict | None
):
    root_id = _mark_manifest(db, test_novel_id)
    path = path_template.format(root_id=root_id, novel_id=test_novel_id)

    response = _request(http_client, method, path, body)

    assert response.status_code == 410
    assert response.json()["detail"] == MANIFEST_DETAIL


STRUCTURE_MUTATIONS = (
    ("post", "/api/v1/novels/{novel_id}/structure/nodes", {"node_type": "part", "number": 1, "title": "Part"}),
    ("put", "/api/v1/novels/{novel_id}/structure/nodes/node-1", {"title": "Renamed"}),
    ("delete", "/api/v1/novels/{novel_id}/structure/nodes/node-1", None),
    ("post", "/api/v1/novels/{novel_id}/structure/reorder", {"node_ids": ["node-1"]}),
    ("post", "/api/v1/novels/{novel_id}/structure/update-ranges", None),
    ("post", "/api/v1/novels/{novel_id}/structure/create-default", {"total_chapters": 10}),
)


@pytest.mark.parametrize(("method", "path_template", "body"), STRUCTURE_MUTATIONS)
def test_manifest_authority_preflights_every_legacy_structure_mutation(
    structure_http_client, db, test_novel_id, method: str, path_template: str, body: dict | None
):
    client, service = structure_http_client
    _mark_manifest(db, test_novel_id)

    response = _request(client, method, path_template.format(novel_id=test_novel_id), body)

    assert response.status_code == 410
    assert response.json()["detail"] == MANIFEST_DETAIL
    assert service.calls == []


@pytest.mark.parametrize(("method", "path_template", "body"), STRUCTURE_MUTATIONS)
def test_legacy_structure_cutover_races_are_translated_to_gone(
    structure_http_client, test_novel_id, method: str, path_template: str, body: dict | None
):
    client, service = structure_http_client
    service.failure = PlanningAuthorityError("manifest cutover won the race")

    response = _request(client, method, path_template.format(novel_id=test_novel_id), body)

    assert response.status_code == 410
    assert response.json()["detail"] == MANIFEST_DETAIL


def test_manifest_structure_reads_remain_available_and_report_authority(
    structure_http_client, db, test_novel_id
):
    client, _service = structure_http_client
    _mark_manifest(db, test_novel_id)

    tree = client.get(f"/api/v1/novels/{test_novel_id}/structure")
    children = client.get(f"/api/v1/novels/{test_novel_id}/structure/children")

    assert tree.status_code == 200
    assert tree.json()["manifest_authority"] is True
    assert children.status_code == 200
    assert children.json()["manifest_authority"] is True


def test_non_manifest_structure_value_errors_keep_their_legacy_status(
    structure_http_client, test_novel_id
):
    client, service = structure_http_client
    service.failure = ValueError("invalid node type")

    response = client.post(
        f"/api/v1/novels/{test_novel_id}/structure/nodes",
        json={"node_type": "part", "number": 1, "title": "Part"},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "invalid node type"
