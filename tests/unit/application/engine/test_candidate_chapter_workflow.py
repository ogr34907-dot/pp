"""Candidate-first workflow keeps unapproved prose out of formal facts and memory."""

from dataclasses import dataclass

import pytest

from application.engine.services.candidate_chapter_workflow import (
    CandidateChapterWorkflowService,
    CandidateWorkflowError,
)
from application.engine.dag.engine import DAGEngine
from application.engine.dag.models import DAGRunResult, NodeResult, get_default_dag
from domain.novel.candidate_chapter import CandidateStatus, GenerationRunState, RunMode
from infrastructure.persistence.database.chapter_candidate_repository import (
    CandidateGateError,
    ChapterCandidateRepository,
)
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


class _NoRequiredEventOutlines(_Outlines):
    def next_published_chapter_context(self, novel_id: str, *, after_chapter: int):
        node, chain = super().next_published_chapter_context(
            novel_id, after_chapter=after_chapter
        )
        chain["chapter"]["payload"]["required_events"] = []
        return node, chain


class _DraftGenerator:
    def __init__(self, text: str = "主角作出选择，代价随之而来。"):
        self.text = text
        self.calls: list[int] = []

    async def generate_candidate_draft(self, *, chapter_number: int, **_kwargs):
        self.calls.append(chapter_number)
        return {"content": self.text, "script": f"第{chapter_number}章剧本"}


class _StreamingDraftGenerator(_DraftGenerator):
    async def generate_candidate_draft(self, *, chapter_number: int, on_event=None, **_kwargs):
        self.calls.append(chapter_number)
        if callable(on_event):
            on_event({"type": "prose_delta", "text": "候选正文片段"})
        return {"content": self.text, "script": f"第{chapter_number}章剧本"}


class _Aftermath:
    def __init__(self):
        self.calls: list[int] = []
        self.kwargs: list[dict] = []

    async def run_after_chapter_saved(self, novel_id, chapter_number, content, **_kwargs):
        self.calls.append(chapter_number)
        self.kwargs.append(dict(_kwargs))
        return {"narrative_sync_ok": True, "memory_engine_ok": True}


class _SemanticReviewer:
    def __init__(self, coverage):
        self.coverage = coverage
        self.calls = []

    async def review(self, **kwargs):
        self.calls.append(kwargs)
        return type(
            "Review",
            (),
            {
                "status": "approved",
                "score": 90,
                "summary": "事件已在正文中完成。",
                "issues": [],
                "suggestions": ["保留动作证据。"],
                "event_coverage": self.coverage,
            },
        )()


class _DAG:
    def __init__(self, states):
        self.states = list(states)
        self.calls = []

    async def run(self, _dag, initial_state, thread_id=""):
        self.calls.append((dict(initial_state), thread_id))
        state = self.states.pop(0)
        return DAGRunResult(
            dag_run_id=initial_state["dag_run_id"],
            novel_id=initial_state["novel_id"],
            status="completed",
            node_results={"workflow": NodeResult(outputs=state)},
        )


class _RuntimeAwareDAG(_DAG):
    async def run(self, _dag, initial_state, thread_id="", *, observer=None, runtime_context=None):
        self.observer = observer
        self.runtime_context = runtime_context
        return await super().run(_dag, initial_state, thread_id)


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
async def test_legacy_baseline_integrity_blocks_the_draft_generator_before_it_is_called(workflow):
    db, repo, drafts, aftermath = workflow
    conn = db.get_connection()
    for number in range(1, 3):
        conn.execute(
            """
            INSERT INTO chapters (id, novel_id, number, title, content, status)
            VALUES (?, 'novel-1', ?, ?, ?, 'completed')
            """,
            (f"legacy-workflow-{number}", number, f"第{number}章", f"旧正文 {number}"),
        )
    conn.commit()
    repo.import_legacy_formal_history("novel-1")
    repo.start_run("novel-1", run_mode=RunMode.CHAPTER_REVIEW, target_chapters=3)
    conn.execute(
        "UPDATE chapters SET content_revision = content_revision + 1 WHERE novel_id = 'novel-1' AND number = 2"
    )
    conn.commit()
    service = CandidateChapterWorkflowService(repo, _Outlines(), drafts, aftermath)

    with pytest.raises(CandidateGateError, match="legacy formal history integrity mismatch"):
        await service.generate_next("novel-1")

    assert drafts.calls == []


@pytest.mark.asyncio
async def test_direct_generation_rechecks_legacy_authority_after_candidate_creation(workflow, monkeypatch):
    db, repo, drafts, aftermath = workflow
    conn = db.get_connection()
    for number in range(1, 3):
        conn.execute(
            """
            INSERT INTO chapters (id, novel_id, number, title, content, status)
            VALUES (?, 'novel-1', ?, ?, ?, 'completed')
            """,
            (f"legacy-direct-{number}", number, f"第{number}章", f"旧正文 {number}"),
        )
    conn.commit()
    repo.import_legacy_formal_history("novel-1")
    repo.start_run("novel-1", run_mode=RunMode.CHAPTER_REVIEW, target_chapters=3)
    create_candidate = repo.create_streaming_candidate

    def create_then_tamper(**kwargs):
        candidate = create_candidate(**kwargs)
        conn.execute(
            "UPDATE chapters SET content_revision = content_revision + 1 "
            "WHERE novel_id = 'novel-1' AND number = 2"
        )
        conn.commit()
        return candidate

    monkeypatch.setattr(repo, "create_streaming_candidate", create_then_tamper)
    service = CandidateChapterWorkflowService(repo, _Outlines(), drafts, aftermath)

    with pytest.raises(CandidateWorkflowError, match="legacy formal history integrity mismatch"):
        await service.generate_next("novel-1")

    assert drafts.calls == []


@pytest.mark.asyncio
async def test_dag_generation_rechecks_legacy_authority_after_candidate_creation(workflow, monkeypatch):
    db, repo, drafts, aftermath = workflow
    conn = db.get_connection()
    for number in range(1, 3):
        conn.execute(
            """
            INSERT INTO chapters (id, novel_id, number, title, content, status)
            VALUES (?, 'novel-1', ?, ?, ?, 'completed')
            """,
            (f"legacy-dag-{number}", number, f"第{number}章", f"旧正文 {number}"),
        )
    conn.commit()
    repo.import_legacy_formal_history("novel-1")
    repo.start_run("novel-1", run_mode=RunMode.CHAPTER_REVIEW, target_chapters=3)
    create_candidate = repo.create_streaming_candidate

    def create_then_tamper(**kwargs):
        candidate = create_candidate(**kwargs)
        conn.execute(
            "UPDATE chapters SET content_revision = content_revision + 1 "
            "WHERE novel_id = 'novel-1' AND number = 2"
        )
        conn.commit()
        return candidate

    monkeypatch.setattr(repo, "create_streaming_candidate", create_then_tamper)
    dag = _DAG([{"content": "不应执行的 DAG 正文", "approved": True}])
    service = CandidateChapterWorkflowService(
        repo, _Outlines(), drafts, aftermath, dag_engine=dag, dag_factory=lambda: object()
    )

    with pytest.raises(CandidateWorkflowError, match="legacy formal history integrity mismatch"):
        await service.generate_next("novel-1")

    assert dag.calls == []


@pytest.mark.asyncio
async def test_canonical_sync_uses_author_final_commit_plan_not_original_outline(workflow):
    _db, repo, drafts, aftermath = workflow
    repo.start_run("novel-1", run_mode=RunMode.CHAPTER_REVIEW, target_chapters=3)
    service = CandidateChapterWorkflowService(repo, _Outlines(), drafts, aftermath)

    candidate = await service.generate_next("novel-1")
    revised_plan = {
        "chapter_summary": "作者确认的最终摘要",
        "timeline_events": ["作者改写后的事件"],
        "next_chapter_handoff": ["下一章从雨夜追捕开始"],
    }
    repo.update_commit_plan(candidate.id, revised_plan)

    committed = await service.accept_candidate(candidate.id, continue_after_commit=False)

    assert committed.status == CandidateStatus.COMMITTED
    assert aftermath.kwargs[-1]["outline"] == "作者确认的最终摘要\n作者改写后的事件\n下一章从雨夜追捕开始"


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
async def test_continuous_mode_waits_when_required_outline_event_has_no_prose_evidence(workflow):
    db, repo, drafts, aftermath = workflow
    repo.start_run("novel-1", run_mode=RunMode.CONTINUOUS, target_chapters=3)
    drafts.text = "主角把手机放回桌上，没有作出决定，只让雨声填满房间。"
    dag = _DAG([{"content": drafts.text, "approved": True, "review_required": False}])
    service = CandidateChapterWorkflowService(
        repo, _Outlines(), drafts, aftermath, dag_engine=dag, dag_factory=lambda: object()
    )

    candidate = await service.generate_next("novel-1")

    required = candidate.audit["plan_actual_comparison"]["required_events"]
    assert candidate.status == CandidateStatus.AWAITING_REVIEW
    assert required == [{"event": "主角作出选择", "status": "unverified", "evidence": ""}]
    assert candidate.commit_plan["timeline_events"] == []
    assert db.fetch_one("SELECT COUNT(*) AS total FROM chapters WHERE novel_id = 'novel-1'")["total"] == 0
    assert aftermath.calls == []


@pytest.mark.asyncio
async def test_semantic_event_evidence_allows_equivalent_prose_to_continue(workflow):
    _db, repo, drafts, aftermath = workflow
    repo.start_run("novel-1", run_mode=RunMode.CONTINUOUS, target_chapters=3)
    drafts.text = "他把退路留在身后，推开雨幕，径直走向约好的港口。"
    reviewer = _SemanticReviewer(
        [{"event": "主角作出选择", "status": "completed", "evidence": "推开雨幕，径直走向约好的港口"}]
    )
    dag = _DAG([{"content": drafts.text, "approved": True, "review_required": False}])
    service = CandidateChapterWorkflowService(
        repo,
        _Outlines(),
        drafts,
        aftermath,
        dag_engine=dag,
        dag_factory=lambda: object(),
        semantic_reviewer=reviewer,
    )

    candidate = await service.generate_next("novel-1")

    assert candidate.status == CandidateStatus.COMMITTED
    assert candidate.audit["plan_actual_comparison"]["required_events"] == [
        {"event": "主角作出选择", "status": "completed", "evidence": "推开雨幕，径直走向约好的港口"}
    ]
    assert candidate.commit_plan["timeline_events"] == ["主角作出选择"]
    assert len(reviewer.calls) == 1
    assert aftermath.calls == [1]


@pytest.mark.asyncio
async def test_candidate_audit_reuses_dag_semantic_evidence_without_a_second_review_call(workflow):
    _db, repo, drafts, aftermath = workflow
    repo.start_run("novel-1", run_mode=RunMode.CONTINUOUS, target_chapters=3)
    drafts.text = "他把退路留在身后，推开雨幕，径直走向约好的港口。"

    class _UnexpectedReviewer:
        async def review(self, **_kwargs):
            raise AssertionError("DAG semantic result must be reused by the candidate audit")

    dag = _DAG([
        {
            "content": drafts.text,
            "approved": True,
            "review_required": False,
            "semantic_review": {
                "status": "approved",
                "event_coverage": [
                    {
                        "event": "主角作出选择",
                        "status": "completed",
                        "evidence": "推开雨幕，径直走向约好的港口",
                    }
                ],
            },
        }
    ])
    service = CandidateChapterWorkflowService(
        repo,
        _Outlines(),
        drafts,
        aftermath,
        dag_engine=dag,
        dag_factory=lambda: object(),
        semantic_reviewer=_UnexpectedReviewer(),
    )

    candidate = await service.generate_next("novel-1")

    assert candidate.status == CandidateStatus.COMMITTED
    assert candidate.audit["semantic_review"]["status"] == "approved"
    assert aftermath.calls == [1]


@pytest.mark.asyncio
async def test_negative_required_event_cannot_pass_from_literal_text_when_semantic_review_is_unverified(
    workflow,
):
    _db, repo, drafts, aftermath = workflow
    repo.start_run("novel-1", run_mode=RunMode.CONTINUOUS, target_chapters=3)
    drafts.text = "沈岚没有交出证据。"

    class _NegativeEventOutlines(_Outlines):
        def next_published_chapter_context(self, novel_id, *, after_chapter):
            node, chain = super().next_published_chapter_context(
                novel_id, after_chapter=after_chapter
            )
            chain["chapter"]["payload"]["required_events"] = ["交出证据"]
            return node, chain

    dag = _DAG(
        [
            {
                "content": drafts.text,
                "approved": True,
                "review_required": False,
                "semantic_review": {
                    "status": "approved",
                    "issues": [],
                    "event_coverage": [
                        {"event": "交出证据", "status": "unverified", "evidence": ""}
                    ],
                },
            }
        ]
    )
    service = CandidateChapterWorkflowService(
        repo,
        _NegativeEventOutlines(),
        drafts,
        aftermath,
        dag_engine=dag,
        dag_factory=lambda: object(),
        max_candidate_revisions=0,
    )

    candidate = await service.generate_next("novel-1")

    assert candidate.status == CandidateStatus.AWAITING_REVIEW
    assert candidate.commit_plan["timeline_events"] == []
    assert aftermath.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("required_events", "forbidden_events", "content", "coverage", "expected_status"),
    [
        pytest.param(
            ["沈岚１２３交出证据"],
            [],
            "沈岚123交出证据。",
            [{"event": "沈岚１２３交出证据", "status": "completed", "evidence": "沈岚１２３交出证据"}],
            CandidateStatus.COMMITTED,
            id="required-layout-and-nfkc-differences",
        ),
        pytest.param(
            [],
            ["沈岚１２３交出证据"],
            "沈岚123交出证据。",
            [],
            CandidateStatus.AWAITING_REVIEW,
            id="forbidden-layout-and-nfkc-differences",
        ),
        pytest.param(
            ["沈岚１２３交出证据！"],
            [],
            "沈岚123交出证据。",
            [{"event": "沈岚１２３交出证据！", "status": "completed", "evidence": "沈岚１２３交出证据！"}],
            CandidateStatus.AWAITING_REVIEW,
            id="punctuation-difference-is-not-equivalent",
        ),
        pytest.param(
            ["沈岚１２３交出证据"],
            [],
            "沈岚123没有交出证据。",
            [{"event": "沈岚１２３交出证据", "status": "completed", "evidence": "沈岚１２３交出证据"}],
            CandidateStatus.AWAITING_REVIEW,
            id="negation-and-content-order-remain-significant",
        ),
    ],
)
async def test_candidate_final_audit_applies_only_safe_evidence_normalization(
    workflow,
    required_events,
    forbidden_events,
    content,
    coverage,
    expected_status,
):
    db, repo, drafts, aftermath = workflow
    repo.start_run("novel-1", run_mode=RunMode.CONTINUOUS, target_chapters=3)
    drafts.text = content

    class _EvidenceOutlines(_Outlines):
        def next_published_chapter_context(self, novel_id, *, after_chapter):
            node, chain = super().next_published_chapter_context(
                novel_id, after_chapter=after_chapter
            )
            chain["chapter"]["payload"]["required_events"] = list(required_events)
            chain["chapter"]["payload"]["forbidden_events"] = list(forbidden_events)
            return node, chain

    dag = _DAG(
        [
            {
                "content": drafts.text,
                "approved": True,
                "review_required": False,
                "semantic_review": {
                    "status": "approved",
                    "issues": [],
                    "event_coverage": coverage,
                },
            }
        ]
    )
    service = CandidateChapterWorkflowService(
        repo,
        _EvidenceOutlines(),
        drafts,
        aftermath,
        dag_engine=dag,
        dag_factory=lambda: object(),
        max_candidate_revisions=0,
    )

    candidate = await service.generate_next("novel-1")

    assert candidate.status == expected_status
    chapter_count = db.fetch_one(
        "SELECT COUNT(*) AS total FROM chapters WHERE novel_id = 'novel-1'"
    )["total"]
    assert chapter_count == (1 if expected_status == CandidateStatus.COMMITTED else 0)
    assert aftermath.calls == ([1] if expected_status == CandidateStatus.COMMITTED else [])
    if required_events:
        required = candidate.audit["plan_actual_comparison"]["required_events"]
        assert required[0]["status"] == (
            "completed" if expected_status == CandidateStatus.COMMITTED else "unverified"
        )
    if forbidden_events:
        assert candidate.audit["hard_blocks"]


@pytest.mark.asyncio
async def test_direct_candidate_path_runs_full_semantic_review_without_required_events(workflow):
    _db, repo, drafts, aftermath = workflow
    repo.start_run("novel-1", run_mode=RunMode.CHAPTER_REVIEW, target_chapters=3)
    reviewer = _SemanticReviewer([])
    service = CandidateChapterWorkflowService(
        repo,
        _NoRequiredEventOutlines(),
        drafts,
        aftermath,
        semantic_reviewer=reviewer,
    )

    candidate = await service.generate_next("novel-1")

    assert candidate.status == CandidateStatus.AWAITING_REVIEW
    assert len(reviewer.calls) == 1
    assert reviewer.calls[0]["required_events"] == []
    assert "AI Taste" in reviewer.calls[0]["generation_hint"]
    assert candidate.audit["semantic_review"]["status"] == "approved"
    assert candidate.audit["semantic_review"]["status"] != "not_needed"
    assert aftermath.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("run_mode", [RunMode.CONTINUOUS, RunMode.CHAPTER_REVIEW])
async def test_direct_candidate_path_exposes_missing_semantic_review_and_stops_both_modes(
    workflow, run_mode
):
    _db, repo, drafts, aftermath = workflow
    repo.start_run("novel-1", run_mode=run_mode, target_chapters=3)
    service = CandidateChapterWorkflowService(
        repo,
        _NoRequiredEventOutlines(),
        drafts,
        aftermath,
    )

    candidate = await service.generate_next("novel-1")

    assert candidate.status == CandidateStatus.AWAITING_REVIEW
    assert candidate.audit["semantic_review"]["status"] == "unavailable"
    assert candidate.audit["semantic_review"]["machine_review_failed"] is True
    assert aftermath.calls == []


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


@pytest.mark.asyncio
async def test_candidate_generation_uses_dag_state_as_the_single_draft_and_audit_authority(workflow):
    _db, repo, drafts, aftermath = workflow
    repo.start_run("novel-1", run_mode=RunMode.CHAPTER_REVIEW, target_chapters=3)
    dag = _DAG([
        {
            "content": "主角作出选择，代价随之而来。",
            "script": "DAG 剧本",
            "drift_alert": False,
            "breaker_status": "closed",
            "approved": True,
            "candidate_proposals": {"summary": "候选提案"},
        },
    ])
    service = CandidateChapterWorkflowService(
        repo, _Outlines(), drafts, aftermath, dag_engine=dag, dag_factory=lambda: object()
    )

    candidate = await service.generate_next("novel-1")

    assert candidate.status == CandidateStatus.AWAITING_REVIEW
    assert candidate.llm_content == "主角作出选择，代价随之而来。"
    assert candidate.audit["dag"]["approved"] is False
    assert candidate.audit["semantic_review"]["status"] == "unavailable"
    assert candidate.audit["semantic_review"]["machine_review_failed"] is True
    assert candidate.commit_plan["dag_proposals"] == {"summary": "候选提案"}
    assert drafts.calls == []
    trace = repo.get_latest_dag_run(candidate.id)
    assert trace["status"] == "completed"
    assert trace["final_state"]["content"] == candidate.llm_content


@pytest.mark.asyncio
async def test_candidate_dag_retry_creates_a_bounded_new_content_revision(workflow):
    _db, repo, drafts, aftermath = workflow
    repo.start_run("novel-1", run_mode=RunMode.CHAPTER_REVIEW, target_chapters=3)
    dag = _DAG([
        {
            "content": "第一版正文",
            "retry_requested": True,
            "retry_feedback": "改正文风",
        },
        {
            "content": "第二版正文，主角作出选择。",
            "breaker_status": "closed",
            "approved": True,
        },
    ])
    service = CandidateChapterWorkflowService(
        repo, _Outlines(), drafts, aftermath, dag_engine=dag, dag_factory=lambda: object(), max_candidate_revisions=2
    )

    candidate = await service.generate_next("novel-1")

    assert candidate.status == CandidateStatus.AWAITING_REVIEW
    assert candidate.content_revision == 2
    assert candidate.llm_content == "第二版正文，主角作出选择。"
    assert [version["content"] for version in repo.list_versions(candidate.id)] == ["第二版正文，主角作出选择。", "第一版正文"]
    assert len(dag.calls) == 2


@pytest.mark.asyncio
async def test_exhausted_candidate_dag_retry_waits_for_author_in_continuous_mode(workflow):
    _db, repo, drafts, aftermath = workflow
    repo.start_run("novel-1", run_mode=RunMode.CONTINUOUS, target_chapters=3)
    dag = _DAG([{
        "content": "最终候选稿，主角作出选择。",
        "retry_requested": False,
        "retry_exhausted": True,
        "review_required": True,
        "approved": False,
    }])
    service = CandidateChapterWorkflowService(
        repo, _Outlines(), drafts, aftermath, dag_engine=dag, dag_factory=lambda: object(), max_candidate_revisions=0
    )

    candidate = await service.generate_next("novel-1")

    assert candidate.status == CandidateStatus.AWAITING_REVIEW
    assert candidate.audit["dag"]["retry_exhausted"] is True
    assert aftermath.calls == []


@pytest.mark.asyncio
async def test_candidate_dag_receives_isolated_runtime_generator_and_observer(workflow):
    _db, repo, drafts, aftermath = workflow
    repo.start_run("novel-1", run_mode=RunMode.CHAPTER_REVIEW, target_chapters=3)
    dag = _RuntimeAwareDAG([{"content": "候选 DAG 正文", "approved": True}])
    service = CandidateChapterWorkflowService(
        repo, _Outlines(), drafts, aftermath, dag_engine=dag, dag_factory=lambda: object()
    )

    candidate = await service.generate_next("novel-1")

    assert candidate.llm_content == "候选 DAG 正文"
    assert dag.runtime_context["candidate_draft_generator"] is drafts
    assert dag.runtime_context["outline_chain"]["chapter"]["payload"]["title"] == "第1章：候选"
    assert dag.runtime_context["content_revision"] == 1
    assert dag.observer is not None
    assert getattr(dag, "_observer", None) is None


@pytest.mark.asyncio
async def test_real_dag_v2_persists_candidate_trace_without_serializing_runtime_services(workflow):
    _db, repo, drafts, aftermath = workflow
    repo.start_run("novel-1", run_mode=RunMode.CHAPTER_REVIEW, target_chapters=3)
    drafts.text = "主角作出选择，带着证物赶往港口。"
    service = CandidateChapterWorkflowService(
        repo,
        _Outlines(),
        drafts,
        aftermath,
        dag_engine=DAGEngine(),
        dag_factory=get_default_dag,
        max_candidate_revisions=0,
    )

    candidate = await service.generate_next("novel-1")

    assert candidate.status == CandidateStatus.AWAITING_REVIEW
    assert candidate.llm_content == drafts.text
    trace = repo.get_latest_dag_run(candidate.id)
    assert trace["status"] == "completed"
    assert "_runtime_context" not in trace["final_state"]
    assert {event["node_id"] for event in trace["events"] if event.get("node_id")} >= {
        "exec_writer",
        "val_narrative",
        "gw_review",
    }


@pytest.mark.asyncio
async def test_candidate_dag_skips_redundant_outline_partition(workflow, monkeypatch):
    _db, repo, drafts, aftermath = workflow
    repo.start_run("novel-1", run_mode=RunMode.CHAPTER_REVIEW, target_chapters=3)
    planning_calls = 0

    async def redundant_planning(*_args, **_kwargs):
        nonlocal planning_calls
        planning_calls += 1
        raise AssertionError("candidate DAG must not run a second planning pass")

    monkeypatch.setattr(
        "application.engine.dag.nodes.planning_chapter_outline_node.build_chapter_execution_plan_async",
        redundant_planning,
    )

    class _RhythmicOutlines(_Outlines):
        def next_published_chapter_context(self, novel_id: str, *, after_chapter: int):
            node, chain = super().next_published_chapter_context(
                novel_id, after_chapter=after_chapter
            )
            chain["chapter"]["payload"]["rhythm"] = {
                "chapter_function": "transition",
                "chapter_goal": "完成选择并承担代价",
                "chapter_delta": "主角带着证物离开",
                "ending_hook": "追兵逼近",
            }
            return node, chain

    service = CandidateChapterWorkflowService(
        repo,
        _RhythmicOutlines(),
        drafts,
        aftermath,
        dag_engine=DAGEngine(),
        dag_factory=get_default_dag,
        max_candidate_revisions=0,
    )

    candidate = await service.generate_next("novel-1")
    trace = repo.get_latest_dag_run(candidate.id)

    assert planning_calls == 0
    assert [beat["description"] for beat in trace["final_state"]["beats"]] == [
        "主角作出选择"
    ]
    assert trace["final_state"]["chapter_rhythm"]["ending_hook"] == "追兵逼近"


@pytest.mark.asyncio
async def test_candidate_dag_does_not_persist_outline_as_fact_or_world_context(workflow):
    _db, repo, drafts, aftermath = workflow
    repo.start_run("novel-1", run_mode=RunMode.CHAPTER_REVIEW, target_chapters=3)
    service = CandidateChapterWorkflowService(
        repo,
        _Outlines(),
        drafts,
        aftermath,
        dag_engine=DAGEngine(),
        dag_factory=get_default_dag,
        max_candidate_revisions=0,
    )

    candidate = await service.generate_next("novel-1")
    trace = repo.get_latest_dag_run(candidate.id)
    node_events = {
        event["node_id"]: event
        for event in trace["events"]
        if event.get("type") == "node_completed"
        and event.get("node_id") in {"ctx_blueprint", "ctx_memory"}
    }

    assert trace["final_state"]["outline_chain"] == candidate.outline_chain
    assert "world_rules" not in node_events["ctx_blueprint"]["outputs"]
    assert "fact_lock" not in node_events["ctx_memory"]["outputs"]


@pytest.mark.asyncio
async def test_real_dag_v2_persists_buffered_candidate_prose_events(workflow):
    _db, repo, _drafts, aftermath = workflow
    repo.start_run("novel-1", run_mode=RunMode.CHAPTER_REVIEW, target_chapters=3)
    drafts = _StreamingDraftGenerator("主角作出选择，带着证物赶往港口。")
    service = CandidateChapterWorkflowService(
        repo,
        _Outlines(),
        drafts,
        aftermath,
        dag_engine=DAGEngine(),
        dag_factory=get_default_dag,
        max_candidate_revisions=0,
    )

    candidate = await service.generate_next("novel-1")

    trace = repo.get_latest_dag_run(candidate.id)
    prose_events = [event for event in trace["events"] if event["type"] == "prose_delta"]
    assert len(prose_events) == 1
    assert prose_events[0]["node_id"] == "exec_writer"
    assert prose_events[0]["text"] == "候选正文片段"


@pytest.mark.asyncio
async def test_real_dag_v2_clean_continuous_candidate_commits_after_machine_approval(workflow):
    _db, repo, drafts, aftermath = workflow
    repo.start_run("novel-1", run_mode=RunMode.CONTINUOUS, target_chapters=3)
    drafts.text = "主角作出选择，带着证物赶往港口。"
    reviewer = _SemanticReviewer(
        [{"event": "主角作出选择", "status": "completed", "evidence": "主角作出选择"}]
    )
    service = CandidateChapterWorkflowService(
        repo,
        _Outlines(),
        drafts,
        aftermath,
        dag_engine=DAGEngine(),
        dag_factory=get_default_dag,
        semantic_reviewer=reviewer,
        max_candidate_revisions=0,
    )

    candidate = await service.generate_next("novel-1")

    assert candidate.status == CandidateStatus.COMMITTED
    assert candidate.audit["dag"]["approved"] is True
    assert candidate.audit["dag"]["semantic_review"]["status"] == "approved"
    assert aftermath.calls == [1]


@pytest.mark.asyncio
async def test_real_dag_v2_continuous_candidate_waits_when_semantic_review_is_unavailable(workflow):
    _db, repo, drafts, aftermath = workflow
    repo.start_run("novel-1", run_mode=RunMode.CONTINUOUS, target_chapters=3)
    drafts.text = "主角作出选择，带着证物赶往港口。"
    service = CandidateChapterWorkflowService(
        repo,
        _Outlines(),
        drafts,
        aftermath,
        dag_engine=DAGEngine(),
        dag_factory=get_default_dag,
        max_candidate_revisions=0,
    )

    candidate = await service.generate_next("novel-1")

    assert candidate.status == CandidateStatus.AWAITING_REVIEW
    assert candidate.audit["dag"]["semantic_review"]["status"] == "unavailable"
    assert candidate.audit["dag"]["semantic_review"]["machine_review_failed"] is True
    assert aftermath.calls == []


@pytest.mark.asyncio
async def test_real_dag_v2_chapter_review_retains_candidate_when_machine_review_is_unavailable(workflow):
    _db, repo, drafts, aftermath = workflow
    repo.start_run("novel-1", run_mode=RunMode.CHAPTER_REVIEW, target_chapters=3)
    drafts.text = "主角作出选择，带着证物赶往港口。"
    service = CandidateChapterWorkflowService(
        repo,
        _Outlines(),
        drafts,
        aftermath,
        dag_engine=DAGEngine(),
        dag_factory=get_default_dag,
        max_candidate_revisions=0,
    )

    candidate = await service.generate_next("novel-1")

    assert candidate.status == CandidateStatus.AWAITING_REVIEW
    assert candidate.audit["dag"]["semantic_review"]["machine_review_failed"] is True
    assert candidate.audit["dag"]["approved"] is False
    assert aftermath.calls == []


@pytest.mark.asyncio
async def test_continuous_candidate_with_explicit_rhythm_cannot_commit_without_semantic_review(workflow):
    _db, repo, drafts, aftermath = workflow
    repo.start_run("novel-1", run_mode=RunMode.CONTINUOUS, target_chapters=3)
    drafts.text = "主角作出选择，带着证物赶往港口。"

    class _RhythmOutlines(_Outlines):
        def next_published_chapter_context(self, novel_id: str, *, after_chapter: int):
            node, chain = super().next_published_chapter_context(
                novel_id, after_chapter=after_chapter
            )
            chain["chapter"]["payload"]["rhythm"] = {
                "chapter_function": "transition",
                "chapter_delta": "目标从逃离改为合作",
            }
            return node, chain

    dag = _DAG(
        [
            {
                "content": drafts.text,
                "approved": True,
                "review_required": False,
            }
        ]
    )
    service = CandidateChapterWorkflowService(
        repo,
        _RhythmOutlines(),
        drafts,
        aftermath,
        dag_engine=dag,
        dag_factory=lambda: object(),
        max_candidate_revisions=0,
    )

    candidate = await service.generate_next("novel-1")

    assert candidate.status == CandidateStatus.AWAITING_REVIEW
    assert candidate.audit["dag"]["approved"] is False
    assert candidate.audit["semantic_review"]["machine_review_failed"] is True
    assert aftermath.calls == []
