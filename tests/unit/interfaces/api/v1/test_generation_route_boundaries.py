"""Candidate state transitions must stay inside the canonical workflow."""

from fastapi.testclient import TestClient

from interfaces.api.v1.engine import generation
from interfaces.api.v1.engine import generation_routes
from interfaces.main import create_app


async def _legacy_prose_stream(*_args, **_kwargs):
    yield {"type": "done", "content": "legacy prose"}


class _LegacyWorkflow:
    generate_chapter_stream = staticmethod(_legacy_prose_stream)


class _LegacyHostedWriteService:
    stream_hosted_write = staticmethod(_legacy_prose_stream)


def test_public_candidate_router_exposes_no_raw_worker_state_transitions():
    paths = {route.path for route in generation_routes.router.routes}

    assert "/generation/novels/{novel_id}/candidates" not in paths
    assert "/generation/candidates/{candidate_id}/generated-content" not in paths
    assert "/generation/candidates/{candidate_id}/audit" not in paths
    assert "/generation/candidates/{candidate_id}/audit-result" not in paths
    assert "/generation/candidates/{candidate_id}/approve" not in paths
    assert "/generation/candidates/{candidate_id}/commit" not in paths
    assert "/generation/candidates/{candidate_id}/sync-succeeded" not in paths
    assert "/generation/candidates/{candidate_id}/sync-failed" not in paths


def test_main_application_rejects_legacy_direct_prose_generation_routes():
    """AI prose must enter the candidate workflow before it can become formal text."""
    app = create_app()
    app.dependency_overrides[generation.get_auto_workflow] = _LegacyWorkflow
    app.dependency_overrides[generation.get_hosted_write_service] = _LegacyHostedWriteService
    client = TestClient(app)

    direct_response = client.post(
        "/api/v1/novels/novel-1/generate-chapter-stream",
        json={"chapter_number": 1, "outline": "legacy outline"},
    )
    hosted_response = client.post(
        "/api/v1/novels/novel-1/hosted-write-stream",
        json={"from_chapter": 1, "to_chapter": 1, "auto_save": True},
    )

    assert direct_response.status_code == 410
    assert hosted_response.status_code == 410
    assert "generation/novels/{novel_id}/start" in direct_response.json()["detail"]
    assert "generation/novels/{novel_id}/start" in hosted_response.json()["detail"]
