"""The public invocation API must not bypass candidate review for prose."""

from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from application.ai_invocation.dtos import InvocationPolicy, InvocationSession, InvocationSessionStatus
from interfaces.api.v1.engine import ai_invocation_routes


LEGACY_DIRECT_PROSE_OPERATIONS = (
    ("chapter.generate", "chapter-generation-main"),
    ("chapter.generate.prose", "chapter-prose-generation"),
    ("autopilot.chapter.prose", "chapter-prose-generation"),
    ("autopilot.prose.from_script", "autopilot-stream-beat"),
)


def _client() -> TestClient:
    app = FastAPI()
    app.include_router(ai_invocation_routes.router)
    return TestClient(app, raise_server_exceptions=False)


@pytest.mark.parametrize(("operation", "node_key"), LEGACY_DIRECT_PROSE_OPERATIONS)
def test_public_invocation_creation_rejects_legacy_direct_prose_before_repository_access(
    monkeypatch,
    operation: str,
    node_key: str,
):
    def unexpected_repository_access():
        raise AssertionError("legacy direct prose must be rejected before repositories are opened")

    monkeypatch.setattr(ai_invocation_routes, "_repositories", unexpected_repository_access)

    response = _client().post(
        "/ai-invocations",
        json={
            "operation": operation,
            "node_key": node_key,
            "policy": "FULL_INTERACTIVE",
            "context": {"novel_id": "novel-1", "chapter_number": 2},
        },
    )

    assert response.status_code == 410
    assert "/api/v1/generation/novels/{novel_id}/start" in response.json()["detail"]


def _legacy_session(operation: str) -> InvocationSession:
    return InvocationSession(
        id="legacy-session",
        operation=operation,
        node_key="legacy-prose-node",
        policy=InvocationPolicy.FULL_INTERACTIVE,
        status=InvocationSessionStatus.AWAITING_PRE_CALL_REVIEW,
        context={"novel_id": "novel-1", "chapter_number": 2},
    )


@pytest.mark.parametrize(("operation", "_node_key"), LEGACY_DIRECT_PROSE_OPERATIONS)
@pytest.mark.parametrize(
    ("action", "payload"),
    (
        ("resume", {"resumed_by": "test"}),
        ("retry", {"resumed_by": "test"}),
        ("accept", {"attempt_id": "legacy-attempt"}),
        ("commits", {"decision_id": "legacy-decision"}),
    ),
)
def test_existing_legacy_prose_sessions_cannot_advance_to_generation_or_commit(
    monkeypatch,
    operation: str,
    _node_key: str,
    action: str,
    payload: dict[str, str],
):
    session = _legacy_session(operation)
    repositories = {"session": SimpleNamespace(get=lambda _session_id: session)}
    monkeypatch.setattr(ai_invocation_routes, "_repositories", lambda: repositories)

    response = _client().post(f"/ai-invocations/{session.id}/{action}", json=payload)

    assert response.status_code == 410
    assert "/api/v1/generation/novels/{novel_id}/start" in response.json()["detail"]
