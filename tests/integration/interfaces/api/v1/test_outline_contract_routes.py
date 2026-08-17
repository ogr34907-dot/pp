"""HTTP surface for the Outline Studio's draft/publish lifecycle."""

from types import SimpleNamespace

from application.blueprint.services.outline_cohort_generation_service import (
    OutlineCohortGenerationError,
)
from infrastructure.persistence.database.outline_contract_repository import OutlineContractRepository
from infrastructure.persistence.database.planning_authority_guard import (
    PlanningAuthorityError,
)
from interfaces.api.v1.blueprint import outline_routes


def test_outline_root_can_be_drafted_then_published_and_synced(client, test_novel_id):
    tree = client.get(f"/api/v1/outline/novels/{test_novel_id}/tree")
    assert tree.status_code == 200
    root = tree.json()["data"]
    assert root["node_type"] == "outline"
    assert root["outline_contract"]["status"] == "missing"

    draft = client.post(
        f"/api/v1/outline/contracts/{root['id']}/draft",
        json={
            "source": "author",
            "payload": {
                "title": "总纲：代价与归来",
                "creative_goal": "主角在牺牲中完成归来",
                "required_events": ["必须失去故乡"],
                "forbidden_events": ["不得无代价获胜"],
            },
        },
    )
    assert draft.status_code == 200
    revision = draft.json()["data"]["draft"]["revision"]

    publish = client.post(
        f"/api/v1/outline/contracts/{root['id']}/publish",
        json={"expected_revision": revision, "idempotency_key": "root-publish-v1"},
    )
    assert publish.status_code == 200
    assert publish.json()["data"]["active"]["status"] == "synced"

    refreshed = client.get(f"/api/v1/outline/novels/{test_novel_id}/tree")
    body = refreshed.json()["data"]
    assert body["title"] == "总纲：代价与归来"
    assert body["outline_contract"]["status"] == "synced"


def test_outline_generation_attempt_can_be_recovered_and_cancelled(client, db, test_novel_id):
    repository = OutlineContractRepository(db)
    root = repository.ensure_root(test_novel_id)
    attempt = repository.start_generation_attempt(
        root.id,
        prompt_snapshot={"system": "outline", "user": "draft"},
        context_digest="outline-context-v1",
    )

    recovered = client.get(
        f"/api/v1/outline/contracts/{root.id}/generation-attempts/latest",
        params={"after_sequence": 0},
    )
    assert recovered.status_code == 200
    assert recovered.json()["data"]["id"] == attempt["id"]
    assert recovered.json()["data"]["events"][0]["type"] == "started"

    cancelled = client.post(
        f"/api/v1/outline/contracts/{root.id}/generation-attempts/{attempt['id']}/cancel"
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["data"]["status"] == "cancelled"


def test_outline_draft_preserves_unknown_payload_fields_in_extra(client, test_novel_id):
    root = client.get(f"/api/v1/outline/novels/{test_novel_id}/tree").json()["data"]
    payload = {
        "title": "Forward-compatible outline",
        "state_changes": {"characters": [{"id": "hero", "to": "resolved"}]},
        "foreshadowing": {"setup": ["a sealed letter"], "payoff": ["the seal breaks"]},
        "extra": {"schema_version": 3},
        "field_provenance": {"title": "manifest-import"},
        "field_locks": {"creative_goal": True},
        "future_schema_field": {"retained": True},
    }

    response = client.post(
        f"/api/v1/outline/contracts/{root['id']}/draft",
        json={"source": "author", "payload": payload},
    )

    assert response.status_code == 200
    saved = response.json()["data"]["draft"]["payload"]
    assert saved["state_changes"] == payload["state_changes"]
    assert saved["foreshadowing"] == payload["foreshadowing"]
    assert saved["extra"] == {
        "schema_version": 3,
        "field_provenance": payload["field_provenance"],
        "field_locks": payload["field_locks"],
        "future_schema_field": payload["future_schema_field"],
    }

    round_trip = client.post(
        f"/api/v1/outline/contracts/{root['id']}/draft",
        json={"source": "author", "payload": saved},
    )
    assert round_trip.status_code == 200
    assert round_trip.json()["data"]["draft"]["payload"] == saved


def test_legacy_outline_publish_without_a_draft_remains_conflict(client, db, test_novel_id):
    root = OutlineContractRepository(db).ensure_root(test_novel_id)

    response = client.post(
        f"/api/v1/outline/contracts/{root.id}/publish",
        json={"expected_revision": 1, "idempotency_key": "missing-draft"},
    )

    assert response.status_code == 409


def test_manifest_cohort_routes_generate_then_publish_without_legacy_node_publish(
    client, test_novel_id
):
    from interfaces.main import app

    class _Service:
        def __init__(self):
            self.calls: list[tuple] = []

        def open_or_clone_cohort_draft(self, novel_id: str):
            assert novel_id == test_novel_id
            return SimpleNamespace(id="draft-plan-1")

        async def generate_cohort(self, **kwargs):
            self.calls.append(("generate", kwargs))
            return {"attempt": {"id": "attempt-1", "status": "completed"}}

        async def publish_completed_cohort(self, *, attempt_id: str):
            self.calls.append(("publish", attempt_id))
            return {"attempt": {"id": attempt_id}, "run": None}

    service = _Service()
    app.dependency_overrides[outline_routes.get_outline_cohort_generation_service] = (
        lambda: service
    )
    try:
        generated = client.post(
            f"/api/v1/outline/novels/{test_novel_id}/cohorts/expand",
            json={
                "parent_logical_node_id": "root",
                "level": "part",
                "author_payloads": [],
            },
        )
        assert generated.status_code == 200
        assert generated.json()["data"]["attempt"]["id"] == "attempt-1"

        published = client.post(
            "/api/v1/outline/cohort-attempts/attempt-1/publish"
        )
        assert published.status_code == 200
        assert published.json()["data"]["attempt"]["id"] == "attempt-1"
        assert service.calls == [
            (
                "generate",
                {
                    "plan_revision_id": "draft-plan-1",
                    "parent_logical_node_id": "root",
                    "level": "part",
                    "author_payloads": (),
                },
            ),
            ("publish", "attempt-1"),
        ]
    finally:
        app.dependency_overrides.pop(
            outline_routes.get_outline_cohort_generation_service, None
        )


def test_manifest_cohort_expand_reuses_the_open_draft_after_a_failed_attempt(
    client, test_novel_id
):
    """A retryable failed LLM attempt must not strand the sole working draft."""

    from interfaces.main import app

    class _Service:
        def open_or_clone_cohort_draft(self, novel_id: str):
            assert novel_id == test_novel_id
            return SimpleNamespace(id="open-draft-1")

        async def generate_cohort(self, **kwargs):
            assert kwargs["plan_revision_id"] == "open-draft-1"
            return {"attempt": {"id": "retry-attempt", "status": "completed"}}

    app.dependency_overrides[outline_routes.get_outline_cohort_generation_service] = (
        _Service
    )
    try:
        response = client.post(
            f"/api/v1/outline/novels/{test_novel_id}/cohorts/expand",
            json={
                "parent_logical_node_id": "root",
                "level": "part",
                "author_payloads": [],
            },
        )

        assert response.status_code == 200
        assert response.json()["data"]["attempt"]["id"] == "retry-attempt"
    finally:
        app.dependency_overrides.pop(
            outline_routes.get_outline_cohort_generation_service, None
        )


def test_manifest_cohort_publish_maps_planning_authority_conflict_to_conflict(
    client, monkeypatch,
):
    from interfaces.main import app

    class _Service:
        async def publish_completed_cohort(self, *, attempt_id: str):
            raise PlanningAuthorityError("manifest cohort state changed")

    class _LegacyRepository:
        def _connection(self):
            return None

        def save_draft(self, *args, **kwargs):
            raise PlanningAuthorityError("legacy draft must remain gone")

        def publish_and_sync(self, *args, **kwargs):
            raise PlanningAuthorityError("legacy publish must remain gone")

    class _LegacyService:
        contract_repository = _LegacyRepository()

        def ensure_contract_for_story_node(self, *args, **kwargs):
                raise AssertionError("legacy bind should be rejected by the guard")

    def _reject_legacy(*args, **kwargs):
        raise PlanningAuthorityError("legacy mutation remains unavailable")

    app.dependency_overrides[outline_routes.get_outline_cohort_generation_service] = (
        _Service
    )
    try:
        response = client.post("/api/v1/outline/cohort-attempts/attempt-1/publish")

        assert response.status_code == 409
        assert response.json()["detail"] == "manifest cohort state changed"

        monkeypatch.setattr(
            outline_routes, "assert_legacy_planning_mutation_allowed", _reject_legacy
        )
        app.dependency_overrides[outline_routes.get_outline_service] = (
            lambda: _LegacyService()
        )

        legacy_draft = client.post(
            "/api/v1/outline/contracts/legacy-contract/draft",
            json={"payload": {}},
        )
        legacy_publish = client.post(
            "/api/v1/outline/contracts/legacy-contract/publish",
            json={"expected_revision": 1},
        )
        legacy_bind = client.post(
            "/api/v1/outline/novels/test-novel/story-nodes/story-1/contract"
        )

        assert legacy_draft.status_code == 410
        assert legacy_publish.status_code == 410
        assert legacy_bind.status_code == 410
    finally:
        app.dependency_overrides.pop(
            outline_routes.get_outline_cohort_generation_service, None
        )
        app.dependency_overrides.pop(outline_routes.get_outline_service, None)


def test_manifest_cohort_publish_maps_service_state_conflict_to_conflict(client):
    from interfaces.main import app

    class _Service:
        async def publish_completed_cohort(self, *, attempt_id: str):
            raise OutlineCohortGenerationError("manifest cohort draft changed")

    app.dependency_overrides[outline_routes.get_outline_cohort_generation_service] = (
        _Service
    )
    try:
        response = client.post("/api/v1/outline/cohort-attempts/attempt-1/publish")

        assert response.status_code == 409
        assert response.json()["detail"] == "manifest cohort draft changed"
    finally:
        app.dependency_overrides.pop(
            outline_routes.get_outline_cohort_generation_service, None
        )
