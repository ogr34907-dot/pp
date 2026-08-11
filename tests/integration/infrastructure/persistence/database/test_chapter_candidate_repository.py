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


def test_stopping_an_inflight_candidate_retires_late_worker_writes(candidates):
    repo, _db = candidates
    candidate = repo.create_streaming_candidate(
        novel_id="novel-1", chapter_number=1, title="第一章", outline_chain=_chain()
    )

    stopped = repo.stop_run("novel-1")

    assert stopped.state == GenerationRunState.STOPPED
    assert stopped.current_candidate_id is None
    assert repo.get_candidate(candidate.id).status == CandidateStatus.CANCELLED
    with pytest.raises(CandidateGateError, match="retired generation epoch"):
        repo.set_generated_content(candidate.id, "终止后迟到的正文")
