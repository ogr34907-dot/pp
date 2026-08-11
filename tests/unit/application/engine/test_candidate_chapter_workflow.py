"""Candidate-first workflow keeps unapproved prose out of formal facts and memory."""

from dataclasses import dataclass

import pytest

from application.engine.services.candidate_chapter_workflow import CandidateChapterWorkflowService
from domain.novel.candidate_chapter import CandidateStatus, GenerationRunState, RunMode
from infrastructure.persistence.database.chapter_candidate_repository import ChapterCandidateRepository
from infrastructure.persistence.database.connection import DatabaseConnection


@dataclass
class _ChapterNode:
    id: str
    number: int
    title: str


class _Outlines:
    def __init__(self, *, forbidden: str = ""):
        self.forbidden = forbidden

    def next_published_chapter_context(self, novel_id: str, *, after_chapter: int):
        number = after_chapter + 1
        return _ChapterNode(f"chapter-node-{number}", number, f"第{number}章：候选"), {
            "outline": {"contract_id": "outline-v1", "revision": 1, "digest": "outline-v1", "payload": {}},
            "part": {"contract_id": "part-v1", "revision": 1, "digest": "part-v1", "payload": {}},
            "volume": {"contract_id": "volume-v1", "revision": 1, "digest": "volume-v1", "payload": {}},
            "act": {"contract_id": "act-v1", "revision": 1, "digest": "act-v1", "payload": {}},
            "chapter": {
                "contract_id": f"chapter-v{number}",
                "revision": 1,
                "digest": f"chapter-v{number}",
                "payload": {
                    "title": f"第{number}章：候选",
                    "creative_goal": "让冲突获得有代价的推进",
                    "required_events": ["主角作出选择"],
                    "forbidden_events": [self.forbidden] if self.forbidden else [],
                    "state_changes": {"characters": [{"id": "hero", "change": "更坚定"}]},
                    "foreshadowing": {"advance": ["旧承诺"]},
                    "handoff_conditions": ["保留下一章危机"],
                },
            },
        }


class _DraftGenerator:
    def __init__(self, text: str = "主角作出选择，代价随之而来。"):
        self.text = text
        self.calls: list[int] = []

    async def generate_candidate_draft(self, *, chapter_number: int, **_kwargs):
        self.calls.append(chapter_number)
        return {"content": self.text, "script": f"第{chapter_number}章剧本"}


class _Aftermath:
    def __init__(self):
        self.calls: list[int] = []

    async def run_after_chapter_saved(self, novel_id, chapter_number, content, **_kwargs):
        self.calls.append(chapter_number)
        return {"narrative_sync_ok": True, "memory_engine_ok": True}


@pytest.fixture
def workflow(tmp_path):
    db = DatabaseConnection(str(tmp_path / "candidate-workflow.db"))
    conn = db.get_connection()
    conn.execute(
        "INSERT INTO novels (id, title, slug, target_chapters) VALUES ('novel-1', '候选小说', 'candidate-workflow', 3)"
    )
    conn.commit()
    repo = ChapterCandidateRepository(db)
    drafts = _DraftGenerator()
    aftermath = _Aftermath()
    return db, repo, drafts, aftermath


@pytest.mark.asyncio
async def test_review_mode_has_exactly_one_candidate_and_no_next_llm_call_until_accepted(workflow):
    db, repo, drafts, aftermath = workflow
    repo.start_run("novel-1", run_mode=RunMode.CHAPTER_REVIEW, target_chapters=3)
    service = CandidateChapterWorkflowService(repo, _Outlines(), drafts, aftermath)

    candidate = await service.generate_next("novel-1")
    assert candidate.status == CandidateStatus.AWAITING_REVIEW
    assert drafts.calls == [1]
    assert db.fetch_one("SELECT COUNT(*) AS total FROM chapters WHERE novel_id = 'novel-1'")["total"] == 0
    assert repo.get_run("novel-1").state == GenerationRunState.WAITING_REVIEW

    committed = await service.accept_candidate(candidate.id, continue_after_commit=True)
    assert committed.status == CandidateStatus.COMMITTED
    assert aftermath.calls == [1]
    assert repo.get_run("novel-1").state == GenerationRunState.RUNNING
    assert drafts.calls == [1]  # acceptance itself does not secretly prefetch Chapter 2

    candidate_two = await service.generate_next("novel-1")
    assert candidate_two.chapter_number == 2
    assert drafts.calls == [1, 2]


@pytest.mark.asyncio
async def test_continuous_mode_stops_at_hard_forbidden_event_instead_of_formal_commit(workflow):
    db, repo, drafts, aftermath = workflow
    repo.start_run("novel-1", run_mode=RunMode.CONTINUOUS, target_chapters=3)
    drafts.text = "主角作出选择后，仍然不得杀人，但这里故意触发该禁令。"
    service = CandidateChapterWorkflowService(repo, _Outlines(forbidden="不得杀人"), drafts, aftermath)

    candidate = await service.generate_next("novel-1")

    assert candidate.status == CandidateStatus.AWAITING_REVIEW
    assert candidate.audit["hard_blocks"]
    assert db.fetch_one("SELECT COUNT(*) AS total FROM chapters WHERE novel_id = 'novel-1'")["total"] == 0
    assert aftermath.calls == []
    assert repo.get_run("novel-1").state == GenerationRunState.WAITING_REVIEW


@pytest.mark.asyncio
async def test_failed_canonical_sync_retries_the_same_formal_candidate_without_new_generation(workflow):
    _db, repo, drafts, aftermath = workflow
    repo.start_run("novel-1", run_mode=RunMode.CHAPTER_REVIEW, target_chapters=3)
    service = CandidateChapterWorkflowService(repo, _Outlines(), drafts, aftermath)
    candidate = await service.generate_next("novel-1")

    async def failed_sync(*_args, **_kwargs):
        return {"narrative_sync_ok": False, "failure_reason": "canonical_aftermath_not_ready"}

    aftermath.run_after_chapter_saved = failed_sync
    failed = await service.accept_candidate(candidate.id, continue_after_commit=True)
    assert failed.status == CandidateStatus.FAILED
    assert drafts.calls == [1]

    async def ready_sync(*_args, **_kwargs):
        return {"narrative_sync_ok": True}

    aftermath.run_after_chapter_saved = ready_sync
    committed = await service.retry_canonical_sync(candidate.id)
    assert committed.status == CandidateStatus.COMMITTED
    assert drafts.calls == [1]


@pytest.mark.asyncio
async def test_author_regeneration_and_reaudit_use_the_same_candidate_without_formal_side_effects(workflow):
    db, repo, drafts, aftermath = workflow
    repo.start_run("novel-1", run_mode=RunMode.CHAPTER_REVIEW, target_chapters=3)
    service = CandidateChapterWorkflowService(repo, _Outlines(), drafts, aftermath)
    candidate = await service.generate_next("novel-1")

    drafts.text = "主角作出选择，并承担新的代价。"
    regenerated = await service.regenerate_candidate(candidate.id, feedback="加强代价")

    assert regenerated.status == CandidateStatus.AWAITING_REVIEW
    assert regenerated.final_content == drafts.text
    assert drafts.calls == [1, 1]
    assert aftermath.calls == []
    assert db.fetch_one("SELECT COUNT(*) AS total FROM chapters WHERE novel_id = 'novel-1'")["total"] == 0

    edited = repo.edit_content(candidate.id, "作者最终修订：主角作出选择。", feedback="删去旁枝")
    assert edited.audit_is_current is False
    reaudited = await service.reaudit_candidate(candidate.id)

    assert reaudited.status == CandidateStatus.AWAITING_REVIEW
    assert reaudited.audit_is_current is True
    assert reaudited.commit_plan_is_current is True
    assert drafts.calls == [1, 1]  # Re-audit never consumes prose-generation tokens.
