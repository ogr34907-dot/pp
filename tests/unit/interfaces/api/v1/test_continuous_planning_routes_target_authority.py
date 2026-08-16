from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from interfaces.api.v1.blueprint import continuous_planning_routes
from infrastructure.persistence.database.planning_authority_guard import (
    PlanningAuthorityError,
)


def _client(service):
    app = FastAPI()
    app.include_router(continuous_planning_routes.router, prefix="/api/v1")
    app.dependency_overrides[continuous_planning_routes.get_service] = lambda: service
    return TestClient(app)


def test_macro_post_ignores_request_target_and_allows_omission():
    service = Mock()
    service.require_persisted_target_chapters.return_value = 120
    service.generate_macro_plan = AsyncMock(return_value={"success": True, "structure": []})

    response = _client(service).post(
        "/api/v1/planning/novels/novel-1/macro/generate",
        json={"target_chapters": 999, "structure": {"parts": 1}},
    )

    assert response.status_code == 202
    service.generate_macro_plan.assert_awaited_once()
    assert "target_chapters" not in service.generate_macro_plan.await_args.kwargs

    service.reset_mock()
    service.generate_macro_plan = AsyncMock(return_value={"success": True, "structure": []})
    omitted = _client(service).post(
        "/api/v1/planning/novels/novel-1/macro/generate",
        json={},
    )
    assert omitted.status_code == 202


def test_macro_post_rejects_invalid_persisted_target_before_task_initialization():
    service = Mock()
    service.require_persisted_target_chapters.side_effect = ValueError(
        "novels.target_chapters must be a positive integer"
    )
    service.generate_macro_plan = AsyncMock(return_value={"success": True, "structure": []})

    response = _client(service).post(
        "/api/v1/planning/novels/novel-1/macro/generate",
        json={"target_chapters": 100},
    )

    assert response.status_code == 400
    assert "target_chapters" in response.json()["detail"]
    service.initialize_macro_plan_task.assert_not_called()
    service.generate_macro_plan.assert_not_awaited()


def test_macro_sse_rejects_invalid_persisted_target_before_llm():
    service = Mock()
    service.require_persisted_target_chapters.side_effect = ValueError(
        "novels.target_chapters must be a positive integer"
    )
    service.generate_macro_plan = AsyncMock(return_value={"success": True, "structure": []})

    response = _client(service).get(
        "/api/v1/planning/novels/novel-1/macro/stream"
    )

    assert response.status_code == 400
    assert "target_chapters" in response.json()["detail"]
    service.initialize_macro_plan_task.assert_not_called()
    service.generate_macro_plan.assert_not_awaited()


def test_continuous_mutation_routes_map_manifest_authority_to_gone():
    error = PlanningAuthorityError("manifest planning authority requires PlanRevision transaction")

    macro = Mock()
    macro.confirm_macro_plan_safe = AsyncMock(side_effect=error)
    response = _client(macro).post(
        "/api/v1/planning/novels/novel-1/macro/confirm",
        json={"structure": []},
    )
    assert response.status_code == 410
    assert response.json()["detail"] == "manifest_planning_authority"

    generate = Mock()
    generate.plan_act_chapters = AsyncMock(side_effect=error)
    response = _client(generate).post(
        "/api/v1/planning/acts/act-1/chapters/generate",
        json={},
    )
    assert response.status_code == 410
    assert response.json()["detail"] == "manifest_planning_authority"

    confirm = Mock()
    confirm.confirm_act_planning = AsyncMock(side_effect=error)
    response = _client(confirm).post(
        "/api/v1/planning/acts/act-1/chapters/confirm",
        json={"chapters": []},
    )
    assert response.status_code == 410
    assert response.json()["detail"] == "manifest_planning_authority"

    continued = Mock()
    continued.continue_planning = AsyncMock(side_effect=error)
    response = _client(continued).post(
        "/api/v1/planning/novels/novel-1/continue",
        json={"current_chapter": 1},
    )
    assert response.status_code == 410
    assert response.json()["detail"] == "manifest_planning_authority"

    create_next = Mock()
    create_next.story_node_repo.get_by_id = AsyncMock(
        return_value=SimpleNamespace(novel_id="novel-1")
    )
    create_next.create_next_act_auto = AsyncMock(side_effect=error)
    response = _client(create_next).post(
        "/api/v1/planning/acts/act-1/create-next"
    )
    assert response.status_code == 410
    assert response.json()["detail"] == "manifest_planning_authority"


def test_act_chapter_stream_rejects_manifest_authority_before_streaming_response():
    service = Mock()
    service.preflight_act_planning_mutation = AsyncMock(
        side_effect=PlanningAuthorityError("manifest planning authority requires PlanRevision transaction")
    )
    service.resolve_act_planning_chapter_count = AsyncMock(return_value=3)
    service.plan_act_chapters = AsyncMock(return_value={"success": True, "chapters": []})

    response = _client(service).get(
        "/api/v1/planning/acts/act-1/chapters/stream"
    )

    assert response.status_code == 410
    assert response.json()["detail"] == "manifest_planning_authority"
    service.resolve_act_planning_chapter_count.assert_not_awaited()
    service.plan_act_chapters.assert_not_awaited()


def test_legacy_continuous_route_keeps_existing_404_and_validation_422_semantics():
    service = Mock()
    service.plan_act_chapters = AsyncMock(side_effect=ValueError("幕节点不存在"))

    missing = _client(service).post(
        "/api/v1/planning/acts/missing/chapters/generate",
        json={},
    )
    invalid = _client(Mock()).post(
        "/api/v1/planning/novels/novel-1/continue",
        json={"current_chapter": 0},
    )

    assert missing.status_code == 404
    assert invalid.status_code == 422
