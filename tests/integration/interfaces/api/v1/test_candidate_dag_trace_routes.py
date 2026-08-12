"""Candidate DAG trace routes expose durable recovery state without replaying prose."""

from domain.novel.candidate_chapter import RunMode
from infrastructure.persistence.database.chapter_candidate_repository import (
    ChapterCandidateRepository,
)


def _create_running_trace(db, novel_id: str) -> tuple[ChapterCandidateRepository, object, dict]:
    repository = ChapterCandidateRepository(db)
    repository.start_run(novel_id, run_mode=RunMode.CHAPTER_REVIEW, target_chapters=3)
    candidate = repository.create_streaming_candidate(
        novel_id=novel_id,
        chapter_number=1,
        title="第一章",
        outline_chain={"outline": {"digest": "outline-v1"}},
    )
    trace = repository.start_dag_run(candidate.id, content_revision=1)
    repository.record_dag_event(
        trace["id"],
        {"type": "node_started", "node_id": "exec_writer", "node_type": "exec_writer"},
    )
    repository.record_dag_event(
        trace["id"],
        {
            "type": "node_completed",
            "node_id": "exec_writer",
            "node_type": "exec_writer",
            "duration_ms": 9,
            "outputs": {"content": "候选正文"},
        },
    )
    return repository, candidate, trace


def test_latest_candidate_dag_trace_replays_only_events_after_durable_cursor(client, db, test_novel_id):
    """A reconnect reads SQLite events rather than process-local SSE history."""

    _repository, candidate, trace = _create_running_trace(db, test_novel_id)

    response = client.get(
        f"/api/v1/generation/candidates/{candidate.id}/dag-runs/latest",
        params={"after_sequence": 1},
    )

    assert response.status_code == 200
    payload = response.json()["data"]
    assert payload["id"] == trace["id"]
    assert payload["candidate_id"] == candidate.id
    assert payload["status"] == "running"
    assert payload["node_attempts"] == [{
        "node_id": "exec_writer",
        "node_type": "exec_writer",
        "status": "completed",
        "duration_ms": 9,
    }]
    assert [event["sequence"] for event in payload["events"]] == [2]


def test_candidate_dag_resume_returns_observation_action_without_replaying_prose(client, db, test_novel_id):
    """A live run is observed, never duplicated by a second resume request."""

    repository, candidate, trace = _create_running_trace(db, test_novel_id)

    response = client.post(
        f"/api/v1/generation/candidates/{candidate.id}/dag-runs/latest/resume"
    )

    assert response.status_code == 200
    payload = response.json()["data"]
    assert payload["candidate"]["id"] == candidate.id
    assert payload["dag_run"]["id"] == trace["id"]
    assert payload["dag_run"]["status"] == "running"
    assert payload["continuation"] == {
        "action": "observe_dag_run",
        "safe_to_start": False,
        "reason": "dag_run_still_running",
    }
    assert repository.get_dag_run(trace["id"])["status"] == "running"


def test_candidate_dag_trace_rejects_a_negative_durable_cursor(client, db, test_novel_id):
    """Invalid cursors fail explicitly instead of silently replaying the full trace."""

    _repository, candidate, _trace = _create_running_trace(db, test_novel_id)

    response = client.get(
        f"/api/v1/generation/candidates/{candidate.id}/dag-runs/latest",
        params={"after_sequence": -1},
    )

    assert response.status_code == 422
    assert response.json()["detail"] == "after_sequence must be greater than or equal to zero"
