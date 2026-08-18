"""Candidate state transitions must stay inside the canonical workflow."""

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from domain.novel.candidate_chapter import GenerationRunState, RunMode
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


@pytest.mark.asyncio
async def test_candidate_commit_success_not_hidden_by_continuation_claim_failure(monkeypatch):
    candidate = SimpleNamespace(
        id="candidate-1",
        novel_id="novel-1",
        chapter_number=1,
        title="第一章",
        generation_epoch=3,
        status=SimpleNamespace(value="committed"),
        outline_chain={},
        outline_chain_digest="outline-v1",
        llm_content="草稿",
        author_content="",
        final_content="正式正文",
        content_revision=1,
        audit_revision=1,
        audit_is_current=True,
        commit_plan_revision=1,
        commit_plan_is_current=True,
        audit={},
        commit_plan={},
        feedback="",
        failure_reason="",
        continue_after_commit=True,
        formal_chapter_id="chapter-1",
    )
    errors = []

    class Service:
        async def accept_candidate(self, candidate_id, *, continue_after_commit):
            assert candidate_id == "candidate-1"
            assert continue_after_commit is True
            return candidate

    class Repository:
        def get_run(self, novel_id):
            assert novel_id == "novel-1"
            return SimpleNamespace(
                run_mode=RunMode.CONTINUOUS,
                generation_epoch=3,
                state=GenerationRunState.RUNNING,
            )

        def record_runner_error(self, novel_id, *, expected_generation_epoch, reason):
            errors.append((novel_id, expected_generation_epoch, reason))

    class Coordinator:
        def claim(self, novel_id):
            assert novel_id == "novel-1"
            return False

    monkeypatch.setattr(
        generation_routes.api_dependencies,
        "get_generation_run_coordinator",
        lambda: Coordinator(),
    )

    result = await generation_routes.approve_and_commit_candidate(
        "candidate-1",
        generation_routes.ApprovalRequest(continue_after_commit=True),
        Service(),
        Repository(),
    )

    assert result["success"] is True
    assert result["data"]["id"] == "candidate-1"
    assert result["data"]["continuation_started"] is False
    assert result["data"]["continuation_error"] == "generation_runner_claim_failed"
    assert errors == [("novel-1", 3, "generation_runner_claim_failed")]
