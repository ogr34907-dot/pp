"""A candidate chapter must never become a formal chapter before approval + sync."""

import pytest

from domain.novel.candidate_chapter import CandidateStatus, GenerationRunState, RunMode
from infrastructure.persistence.database.chapter_candidate_repository import (
    CandidateGateError,
    ChapterCandidateRepository,
)
from infrastructure.persistence.database.connection import DatabaseConnection


@pytest.fixture
def candidates(tmp_path):
    db = DatabaseConnection(str(tmp_path / "candidates.db"))
    conn = db.get_connection()
    conn.execute(
        "INSERT INTO novels (id, title, slug, target_chapters) VALUES (?, ?, ?, ?)",
        ("novel-1", "Candidate Novel", "candidate-novel", 20),
    )
    conn.commit()
    repo = ChapterCandidateRepository(db)
    repo.start_run("novel-1", run_mode=RunMode.CHAPTER_REVIEW, target_chapters=20)
    return repo, db


def _chain():
    return {"outline": {"contract_id": "root", "revision": 1, "digest": "root-v1"}}


def test_review_mode_enforces_one_pending_candidate_and_never_prefetches(candidates):
    repo, _ = candidates
    candidate = repo.create_streaming_candidate(
        novel_id="novel-1", chapter_number=1, title="第一章", outline_chain=_chain()
    )
    assert candidate.status == CandidateStatus.STREAMING
    assert repo.get_run("novel-1").max_pending_candidates == 1
    assert repo.get_run("novel-1").prefetch == 0

    with pytest.raises(CandidateGateError, match="pending candidate"):
        repo.create_streaming_candidate(
            novel_id="novel-1", chapter_number=2, title="第二章", outline_chain=_chain()
        )

    repo.set_generated_content(candidate.id, "AI 完整候选稿")
    repo.mark_auditing(candidate.id)
    reviewed = repo.finish_audit(
        candidate.id,
        audit={"alignment": "pass"},
        commit_plan={"summary": "主角决定离乡", "facts": [{"type": "event"}]},
    )
    assert reviewed.status == CandidateStatus.AWAITING_REVIEW
    assert repo.get_run("novel-1").state == GenerationRunState.WAITING_REVIEW

    # Waiting for a human review is a strict token boundary: no Chapter 2 work.
    with pytest.raises(CandidateGateError, match="waiting_review"):
        repo.create_streaming_candidate(
            novel_id="novel-1", chapter_number=2, title="第二章", outline_chain=_chain()
        )


def test_start_run_resumes_formal_cursor_after_synced_candidate_commits(tmp_path):
    db = DatabaseConnection(str(tmp_path / "existing-chapters.db"))
    conn = db.get_connection()
    conn.execute(
        "INSERT INTO novels (id, title, slug, target_chapters) VALUES (?, ?, ?, ?)",
        ("novel-existing", "Existing Novel", "existing-novel", 20),
    )
    conn.commit()

    repo = ChapterCandidateRepository(db)
    repo.start_run(
        "novel-existing", run_mode=RunMode.CHAPTER_REVIEW, target_chapters=20
    )
    for number in (1, 2):
        candidate = repo.create_streaming_candidate(
            novel_id="novel-existing",
            chapter_number=number,
            title=f"第{number}章",
            outline_chain=_chain(),
            llm_content=f"正文{number}",
        )
        repo.mark_auditing(candidate.id)
        repo.finish_audit(candidate.id, audit={}, commit_plan={})
        repo.approve_for_commit(candidate.id, continue_after_commit=number == 1)
        repo.commit_formal(candidate.id)
        repo.mark_sync_succeeded(candidate.id)

    run = repo.start_run(
        "novel-existing", run_mode=RunMode.CHAPTER_REVIEW, target_chapters=20
    )

    assert run.current_formal_chapter == 2
    candidate = repo.create_streaming_candidate(
        novel_id="novel-existing", chapter_number=3, title="第三章", outline_chain=_chain()
    )
    assert candidate.chapter_number == 3


def test_start_run_ignores_empty_and_uncommitted_draft_chapters(tmp_path):
    """Draft placeholders must never move the durable formal cursor."""

    db = DatabaseConnection(str(tmp_path / "draft-placeholders.db"))
    conn = db.get_connection()
    conn.execute(
        "INSERT INTO novels (id, title, slug, target_chapters) VALUES (?, ?, ?, ?)",
        ("novel-drafts", "Draft Novel", "draft-novel", 20),
    )
    for number, content in ((1, ""), (2, "临时草稿"), (100, "")):
        conn.execute(
            """
            INSERT INTO chapters (id, novel_id, number, title, content, status)
            VALUES (?, 'novel-drafts', ?, ?, ?, 'draft')
            """,
            (f"draft-{number}", number, f"第{number}章", content),
        )
    conn.commit()

    repo = ChapterCandidateRepository(db)
    run = repo.start_run(
        "novel-drafts", run_mode=RunMode.CHAPTER_REVIEW, target_chapters=20
    )

    assert run.current_formal_chapter == 0
    candidate = repo.create_streaming_candidate(
        novel_id="novel-drafts", chapter_number=1, title="第一章", outline_chain=_chain()
    )
    assert candidate.chapter_number == 1


def test_create_candidate_requires_the_immediate_next_formal_chapter(candidates):
    repo, db = candidates
    db.execute(
        "UPDATE novel_generation_runs SET current_formal_chapter = 2 WHERE novel_id = 'novel-1'"
    )
    db.get_connection().commit()

    with pytest.raises(CandidateGateError, match="immediate next chapter"):
        repo.create_streaming_candidate(
            novel_id="novel-1", chapter_number=4, title="第四章", outline_chain=_chain()
        )


def test_stopped_review_candidate_must_be_resolved_before_a_new_run(candidates):
    repo, _db = candidates
    candidate = repo.create_streaming_candidate(
        novel_id="novel-1", chapter_number=1, title="第一章", outline_chain=_chain(), llm_content="候选"
    )
    repo.mark_auditing(candidate.id)
    repo.finish_audit(candidate.id, audit={}, commit_plan={})
    stopped = repo.stop_run("novel-1")

    assert stopped.state == GenerationRunState.STOPPED
    assert stopped.current_candidate_id == candidate.id
    with pytest.raises(CandidateGateError, match="pending candidate"):
        repo.start_run("novel-1", run_mode=RunMode.CHAPTER_REVIEW, target_chapters=3)
    assert repo.get_run("novel-1").state == GenerationRunState.STOPPED


def test_author_edit_stales_audit_and_commit_plan_until_reaudited(candidates):
    repo, db = candidates
    candidate = repo.create_streaming_candidate(
        novel_id="novel-1", chapter_number=1, title="第一章", outline_chain=_chain(), llm_content="AI 初稿"
    )
    repo.mark_auditing(candidate.id)
    repo.finish_audit(candidate.id, audit={"alignment": "pass"}, commit_plan={"summary": "旧摘要"})

    edited = repo.edit_content(candidate.id, "作者修订稿", feedback="删掉巧合")
    assert edited.content_revision == 2
    assert edited.audit_is_current is False
    assert edited.commit_plan_is_current is False
    assert db.fetch_one("SELECT COUNT(*) AS total FROM chapters WHERE novel_id = ?", ("novel-1",))["total"] == 0

    with pytest.raises(CandidateGateError, match="re-audit"):
        repo.approve_for_commit(candidate.id, continue_after_commit=True)

    repo.mark_auditing(candidate.id)
    repo.finish_audit(candidate.id, audit={"alignment": "pass"}, commit_plan={"summary": "新摘要"})
    repo.approve_for_commit(candidate.id, continue_after_commit=True)
    syncing = repo.commit_formal(candidate.id)
    assert syncing.status == CandidateStatus.SYNCING
    assert db.fetch_one("SELECT content FROM chapters WHERE novel_id = ? AND number = 1", ("novel-1",))["content"] == "作者修订稿"

    committed = repo.mark_sync_succeeded(candidate.id)
    assert committed.status == CandidateStatus.COMMITTED
    assert repo.get_run("novel-1").state == GenerationRunState.RUNNING
    assert repo.get_run("novel-1").current_formal_chapter == 1


def test_retired_generation_cannot_apply_late_candidate_worker_writes(candidates):
    repo, db = candidates
    candidate = repo.create_streaming_candidate(
        novel_id="novel-1", chapter_number=1, title="第一章", outline_chain=_chain()
    )
    db.execute(
        "UPDATE novel_generation_runs SET generation_epoch = generation_epoch + 1 WHERE novel_id = 'novel-1'"
    )
    db.get_connection().commit()

    with pytest.raises(CandidateGateError, match="retired generation epoch"):
        repo.set_generated_content(candidate.id, "迟到的旧任务正文")

    assert repo.get_candidate(candidate.id).llm_content == ""


def test_review_candidate_can_be_regenerated_or_rejected_without_formal_side_effects(candidates):
    repo, db = candidates
    candidate = repo.create_streaming_candidate(
        novel_id="novel-1", chapter_number=1, title="第一章", outline_chain=_chain(), llm_content="旧候选"
    )
    repo.mark_auditing(candidate.id)
    repo.finish_audit(candidate.id, audit={"alignment": "warn"}, commit_plan={"summary": "旧"})

    regenerating = repo.request_regeneration(candidate.id, feedback="重写冲突段")
    assert regenerating.status == CandidateStatus.REGENERATING
    assert repo.get_run("novel-1").state == GenerationRunState.RUNNING
    regenerated = repo.set_generated_content(candidate.id, "新候选")
    assert regenerated.final_content == "新候选"

    repo.mark_auditing(candidate.id)
    repo.finish_audit(candidate.id, audit={"alignment": "pass"}, commit_plan={"summary": "新"})
    rejected = repo.reject_and_stop(candidate.id)
    assert rejected.status == CandidateStatus.REJECTED
    assert repo.get_run("novel-1").state == GenerationRunState.STOPPED
    assert db.fetch_one("SELECT COUNT(*) AS total FROM chapters WHERE novel_id = ?", ("novel-1",))["total"] == 0


def test_rejecting_an_inflight_candidate_cancels_its_dag_trace(candidates):
    repo, db = candidates
    candidate = repo.create_streaming_candidate(
        novel_id="novel-1", chapter_number=1, title="第一章", outline_chain=_chain()
    )
    trace = repo.start_dag_run(candidate.id, content_revision=0)

    rejected = repo.reject_and_stop(candidate.id)

    assert rejected.status == CandidateStatus.REJECTED
    assert repo.get_run("novel-1").generation_epoch == 1
    assert db.fetch_one(
        "SELECT status FROM candidate_dag_runs WHERE id = ?", (trace["id"],)
    )["status"] == "cancelled"
    with pytest.raises(CandidateGateError, match="not active"):
        repo.finish_dag_run(trace["id"], status="completed", final_state={})


def test_formal_candidate_with_failed_sync_can_retry_without_duplicate_chapter(candidates):
    repo, db = candidates
    candidate = repo.create_streaming_candidate(
        novel_id="novel-1", chapter_number=1, title="第一章", outline_chain=_chain(), llm_content="候选"
    )
    repo.mark_auditing(candidate.id)
    repo.finish_audit(candidate.id, audit={}, commit_plan={})
    repo.approve_for_commit(candidate.id, continue_after_commit=False)
    repo.commit_formal(candidate.id)
    failed = repo.mark_sync_failed(candidate.id, "canonical_aftermath_not_ready")
    assert failed.status == CandidateStatus.FAILED

    retrying = repo.begin_sync_retry(candidate.id)
    assert retrying.status == CandidateStatus.SYNCING
    repo.mark_sync_succeeded(candidate.id)
    assert db.fetch_one("SELECT COUNT(*) AS total FROM chapters WHERE novel_id = ?", ("novel-1",))["total"] == 1


def test_formal_candidate_with_failed_sync_cannot_be_rejected(candidates):
    repo, _db = candidates
    candidate = repo.create_streaming_candidate(
        novel_id="novel-1", chapter_number=1, title="第一章", outline_chain=_chain(), llm_content="候选"
    )
    repo.mark_auditing(candidate.id)
    repo.finish_audit(candidate.id, audit={}, commit_plan={})
    repo.approve_for_commit(candidate.id, continue_after_commit=False)
    repo.commit_formal(candidate.id)
    repo.mark_sync_failed(candidate.id, "canonical_aftermath_not_ready")

    with pytest.raises(CandidateGateError, match="formal candidate"):
        repo.reject_and_stop(candidate.id)

    assert repo.get_candidate(candidate.id).formal_chapter_id is not None
    assert repo.get_run("novel-1").next_action == "retry_sync"


def test_stopping_during_canonical_sync_finishes_sync_then_pauses(candidates):
    repo, _db = candidates
    candidate = repo.create_streaming_candidate(
        novel_id="novel-1", chapter_number=1, title="第一章", outline_chain=_chain(), llm_content="候选"
    )
    repo.mark_auditing(candidate.id)
    repo.finish_audit(candidate.id, audit={}, commit_plan={})
    repo.approve_for_commit(candidate.id, continue_after_commit=True)
    repo.commit_formal(candidate.id)

    stopped = repo.stop_run("novel-1")

    assert stopped.state == GenerationRunState.PAUSED
    assert stopped.current_candidate_id == candidate.id
    assert stopped.next_action == "finish_sync_then_pause"
    assert repo.get_candidate(candidate.id).status == CandidateStatus.SYNCING

    committed = repo.mark_sync_succeeded(candidate.id)
    assert committed.status == CandidateStatus.COMMITTED
    resumed = repo.get_run("novel-1")
    assert resumed.state == GenerationRunState.PAUSED
    assert resumed.current_formal_chapter == 1
    assert resumed.current_candidate_id is None


def test_stopping_an_inflight_candidate_retires_late_worker_writes(candidates):
    repo, db = candidates
    candidate = repo.create_streaming_candidate(
        novel_id="novel-1", chapter_number=1, title="第一章", outline_chain=_chain()
    )
    trace = repo.start_dag_run(candidate.id, content_revision=0)

    stopped = repo.stop_run("novel-1")

    assert stopped.state == GenerationRunState.STOPPED
    assert stopped.current_candidate_id is None
    assert repo.get_candidate(candidate.id).status == CandidateStatus.CANCELLED
    assert db.fetch_one(
        "SELECT status FROM candidate_dag_runs WHERE id = ?", (trace["id"],)
    )["status"] == "cancelled"
    with pytest.raises(CandidateGateError, match="retired generation epoch"):
        repo.set_generated_content(candidate.id, "终止后迟到的正文")


def test_candidate_dag_trace_persists_node_attempts_and_resumable_events(candidates):
    repo, _db = candidates
    candidate = repo.create_streaming_candidate(
        novel_id="novel-1", chapter_number=1, title="第一章", outline_chain=_chain()
    )

    trace = repo.start_dag_run(candidate.id, content_revision=1)
    repo.record_dag_event(
        trace["id"],
        {"type": "node_started", "node_id": "exec_writer", "node_type": "exec_writer"},
    )
    repo.record_dag_event(
        trace["id"],
        {
            "type": "node_completed",
            "node_id": "exec_writer",
            "node_type": "exec_writer",
            "duration_ms": 12,
            "outputs": {"content": "候选正文"},
        },
    )
    repo.finish_dag_run(
        trace["id"],
        status="completed",
        final_state={"content": "候选正文", "review_required": True},
    )

    restored = repo.get_latest_dag_run(candidate.id)

    assert restored["status"] == "completed"
    assert restored["current_node_id"] == "exec_writer"
    assert restored["final_state"]["content"] == "候选正文"
    assert restored["node_attempts"] == [{
        "node_id": "exec_writer",
        "node_type": "exec_writer",
        "status": "completed",
        "duration_ms": 12,
    }]
    assert [event["sequence"] for event in restored["events"]] == [1, 2]


def test_completed_dag_trace_rejects_late_events_and_second_completion(candidates):
    repo, _db = candidates
    candidate = repo.create_streaming_candidate(
        novel_id="novel-1", chapter_number=1, title="第一章", outline_chain=_chain()
    )
    trace = repo.start_dag_run(candidate.id, content_revision=0)
    repo.finish_dag_run(trace["id"], status="completed", final_state={"content": "候选正文"})

    with pytest.raises(CandidateGateError, match="not active"):
        repo.record_dag_event(
            trace["id"], {"type": "node_completed", "node_id": "late_writer"}
        )
    with pytest.raises(CandidateGateError, match="not active"):
        repo.finish_dag_run(trace["id"], status="failed", final_state={})

    restored = repo.get_dag_run(trace["id"])
    assert restored["status"] == "completed"
    assert restored["events"] == []


def test_service_restart_cancels_active_candidate_and_dag_trace(candidates):
    repo, db = candidates
    candidate = repo.create_streaming_candidate(
        novel_id="novel-1", chapter_number=1, title="第一章", outline_chain=_chain()
    )
    trace = repo.start_dag_run(candidate.id, content_revision=0)

    recovered = repo.recover_after_service_restart("novel-1")

    assert recovered.state == GenerationRunState.STOPPED
    assert recovered.current_candidate_id is None
    assert recovered.generation_epoch == 1
    assert repo.get_candidate(candidate.id).status == CandidateStatus.CANCELLED
    assert db.fetch_one(
        "SELECT status FROM candidate_dag_runs WHERE id = ?", (trace["id"],)
    )["status"] == "cancelled"

    with pytest.raises(CandidateGateError, match="not active"):
        repo.finish_dag_run(trace["id"], status="completed", final_state={"content": "迟到"})

    with pytest.raises(CandidateGateError, match="retired generation epoch"):
        repo.set_generated_content(candidate.id, "迟到正文")


def test_service_restart_preserves_candidate_waiting_for_author_review(candidates):
    repo, _db = candidates
    candidate = repo.create_streaming_candidate(
        novel_id="novel-1", chapter_number=1, title="第一章", outline_chain=_chain(), llm_content="候选"
    )
    repo.mark_auditing(candidate.id)
    repo.finish_audit(candidate.id, audit={}, commit_plan={})
    before = repo.get_run("novel-1")

    recovered = repo.recover_after_service_restart("novel-1")

    assert recovered.state == GenerationRunState.WAITING_REVIEW
    assert recovered.current_candidate_id == candidate.id
    assert recovered.generation_epoch == before.generation_epoch
    assert repo.get_candidate(candidate.id).status == CandidateStatus.AWAITING_REVIEW


def test_service_restart_marks_syncing_candidate_retryable_without_retiring_epoch(candidates):
    repo, _db = candidates
    candidate = repo.create_streaming_candidate(
        novel_id="novel-1", chapter_number=1, title="第一章", outline_chain=_chain(), llm_content="候选"
    )
    repo.mark_auditing(candidate.id)
    repo.finish_audit(candidate.id, audit={}, commit_plan={})
    repo.approve_for_commit(candidate.id, continue_after_commit=True)
    repo.commit_formal(candidate.id)
    before = repo.get_run("novel-1")

    recovered = repo.recover_after_service_restart("novel-1")

    assert recovered.state == GenerationRunState.PAUSED
    assert recovered.canonical_sync_status == "failed"
    assert recovered.next_action == "retry_sync"
    assert recovered.generation_epoch == before.generation_epoch
    assert repo.get_candidate(candidate.id).status == CandidateStatus.FAILED
    assert repo.begin_sync_retry(candidate.id).status == CandidateStatus.SYNCING
