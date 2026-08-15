from unittest.mock import AsyncMock, Mock

from fastapi import FastAPI
from fastapi.testclient import TestClient

from interfaces.api.v1.blueprint import continuous_planning_routes


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
