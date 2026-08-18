"""HTTP surface for the Outline Studio's draft/publish lifecycle."""

from types import SimpleNamespace

import pytest

from application.blueprint.services.outline_cohort_generation_service import (
    OutlineCohortGenerationError,
)
from domain.novel.candidate_chapter import GenerationRunState, RunMode
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
                "retry_attempt_id": "failed-attempt",
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


@pytest.mark.asyncio
async def test_manifest_publish_success_not_hidden_by_continuation_claim_failure(monkeypatch):
    from interfaces.main import app

    class _Service:
        async def publish_completed_cohort(self, *, attempt_id: str):
            assert attempt_id == "attempt-1"
            return {
                "attempt": {"id": attempt_id, "status": "published"},
                "run": SimpleNamespace(novel_id="novel-1", generation_epoch=7),
            }

    class _Coordinator:
        def claim(self, novel_id):
            assert novel_id == "novel-1"
            raise RuntimeError("runner unavailable")

    class _Repository:
        def __init__(self, _db):
            pass

        def get_run(self, novel_id):
            assert novel_id == "novel-1"
            return SimpleNamespace(
                state=GenerationRunState.RUNNING,
                generation_epoch=7,
            )

        def record_runner_error(self, novel_id, *, expected_generation_epoch, reason):
            assert (novel_id, expected_generation_epoch, reason) == (
                "novel-1",
                7,
                "runtime_publish_runner_claim_failed:runner unavailable",
            )

    app.dependency_overrides[outline_routes.get_outline_cohort_generation_service] = (
        lambda: _Service()
    )
    monkeypatch.setattr(
        outline_routes.api_dependencies,
        "get_generation_run_coordinator",
        lambda: _Coordinator(),
    )
    monkeypatch.setattr(outline_routes.api_dependencies, "get_database", lambda: object())
    monkeypatch.setattr(outline_routes, "ChapterCandidateRepository", _Repository)
    try:
        result = await outline_routes.publish_manifest_cohort("attempt-1", _Service())

        assert result["success"] is True
        assert result["data"]["attempt"]["id"] == "attempt-1"
        assert result["data"]["continuation_started"] is False
        assert result["data"]["continuation_error"] == (
            "runtime_publish_runner_claim_failed:runner unavailable"
        )
    finally:
        app.dependency_overrides.pop(
            outline_routes.get_outline_cohort_generation_service, None
        )


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


def test_working_tree_and_item_routes_are_manifest_native(client, db, test_novel_id):
    from domain.structure.outline_contract import OutlineLevel, OutlinePayload, OutlineSource

    repository = OutlineContractRepository(db)
    root = repository.ensure_root(test_novel_id)
    draft = repository.save_draft(
        root.id,
        OutlinePayload(
            title="总纲",
            narrative_text="根计划",
            creative_goal="完成主线",
            entry_state="开始",
            exit_state="结束",
        ),
        source=OutlineSource.AUTHOR,
    )
    published = repository.publish_and_sync(
        root.id, expected_revision=draft.draft.revision, idempotency_key="root-v1"
    )
    plan = repository.backfill_initial_plan(test_novel_id).plan
    assert plan is not None
    conn = db.get_connection()
    conn.execute(
        "UPDATE outline_planning_heads SET authority_mode='manifest', "
        "authority_generation=1, projection_generation=1 "
        "WHERE novel_id=?",
        (test_novel_id,),
    )
    conn.commit()
    working = repository.clone_active_plan_draft(test_novel_id)
    working = repository.replace_draft_cohort_payloads(
        plan_revision_id=working.id,
        parent_logical_node_id=published.id,
        payloads=(
            OutlinePayload(
                title="第一部",
                narrative_text="部纲",
                creative_goal="推进",
                entry_state="开始",
                exit_state="结束",
                chapter_start=1,
                chapter_end=10,
            ),
        ),
    )

    response = client.get(f"/api/v1/outline/novels/{test_novel_id}/working-tree")
    assert response.status_code == 200
    tree = response.json()["data"]
    assert tree["plan_revision_id"] == working.id
    assert tree["status"] == "draft"
    part = tree["children"][0]

    edited = client.patch(
        f"/api/v1/outline/plan-revisions/{working.id}/items/{part['logical_node_id']}",
        json={
            "payload": {"title": "第一部（作者修改）", "future_field": {"keep": True}},
            "expected_plan_digest": working.digest,
            "expected_version_digest": part["version_digest"],
        },
    )
    assert edited.status_code == 200
    assert edited.json()["data"]["payload"]["title"] == "第一部（作者修改）"
    assert edited.json()["data"]["payload"]["extra"]["future_field"] == {"keep": True}


def test_author_publish_route_is_separate_from_runtime_publish(client, test_novel_id):
    from interfaces.main import app

    class _Service:
        def __init__(self):
            self.calls = []

        async def publish_author_planning_cohort(self, *, attempt_id: str):
            self.calls.append(attempt_id)
            return {"attempt": {"id": attempt_id}, "run": None}

    service = _Service()
    app.dependency_overrides[outline_routes.get_outline_cohort_generation_service] = (
        lambda: service
    )
    try:
        response = client.post(
            "/api/v1/outline/cohort-attempts/attempt-author/author-publish"
        )
        assert response.status_code == 200
        assert response.json()["data"]["run"] is None
        assert service.calls == ["attempt-author"]
    finally:
        app.dependency_overrides.pop(
            outline_routes.get_outline_cohort_generation_service, None
        )


def test_author_publish_actual_service_does_not_require_a_generation_run(
    client, db, test_novel_id
):
    from application.blueprint.services.outline_cohort_generation_service import (
        OutlineCohortGenerationService,
    )
    from application.blueprint.services.outline_contract_service import OutlineContractService
    from domain.structure.outline_contract import OutlineLevel, OutlinePayload, OutlineSource
    from infrastructure.persistence.database.story_node_repository import StoryNodeRepository
    from interfaces.main import app

    repository = OutlineContractRepository(db)
    root = repository.ensure_root(test_novel_id)
    root_draft = repository.save_draft(
        root.id,
        OutlinePayload(
            title="总纲",
            narrative_text="主线",
            creative_goal="完成目标",
            entry_state="开始",
            exit_state="结束",
        ),
        source=OutlineSource.AUTHOR,
    )
    repository.publish_and_sync(
        root.id, expected_revision=root_draft.draft.revision, idempotency_key="author-root"
    )
    plan = repository.backfill_initial_plan(test_novel_id).plan
    assert plan is not None
    conn = db.get_connection()
    conn.execute(
        "UPDATE outline_planning_heads SET authority_mode='manifest', "
        "authority_generation=1, projection_generation=1 WHERE novel_id=?",
        (test_novel_id,),
    )
    conn.commit()
    working = repository.clone_active_plan_draft(test_novel_id)
    parent_digest = next(
        item for item in working.items if item.logical_node_id == root.id
    ).version_digest
    attempt = repository.start_manifest_cohort_attempt(
        plan_revision_id=working.id,
        parent_logical_node_id=root.id,
        level=OutlineLevel.PART,
        scope={
            "plan_digest": working.digest,
            "parent_logical_node_id": root.id,
            "parent_version_digest": parent_digest,
            "level": "part",
        },
        context_digest="author-context",
        prompt_snapshot={"system": "test", "user": "test"},
    )
    repository.complete_manifest_cohort_attempt_with_payloads(
        attempt_id=attempt["id"],
        payloads=(
            OutlinePayload(
                title="第一部",
                narrative_text="部纲",
                creative_goal="推进",
                entry_state="开始",
                exit_state="结束",
                chapter_start=1,
                chapter_end=10,
            ),
        ),
        expected_plan_digest=working.digest,
        expected_parent_digest=parent_digest,
        expected_context_digest="author-context",
    )
    service = OutlineCohortGenerationService(
        repository,
        OutlineContractService(
            contract_repository=repository,
            story_node_repository=StoryNodeRepository(db),
        ),
        llm_service=object(),
        db=db,
    )
    app.dependency_overrides[outline_routes.get_outline_cohort_generation_service] = (
        lambda: service
    )
    try:
        response = client.post(
            f"/api/v1/outline/cohort-attempts/{attempt['id']}/author-publish"
        )
        assert response.status_code == 200
        assert response.json()["data"]["run"] is None
        assert repository.get_planning_head(test_novel_id).working_plan_revision_id is None
        replay = client.post(
            f"/api/v1/outline/cohort-attempts/{attempt['id']}/author-publish"
        )
        assert replay.status_code == 200
        assert replay.json()["data"]["run"] is None
    finally:
        app.dependency_overrides.pop(
            outline_routes.get_outline_cohort_generation_service, None
        )
