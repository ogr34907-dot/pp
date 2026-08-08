import pytest

from application.engine.services.hierarchical_narrative_alignment_gate import AlignmentReport, AlignmentViolation
from application.engine.services.narrative_gate_guard import evaluate_chapter_candidate
from application.workflows.auto_novel_generation_workflow import AutoNovelGenerationWorkflow


class _Gate:
    memory_engine = None

    def build_snapshot(self, chapter_id, nodes, **kwargs):
        return {"chapter_id": chapter_id}

    async def evaluate(self, snapshot, candidate):
        return AlignmentReport(
            decision="review",
            confidence=0.2,
            candidate_digest="candidate-digest",
            snapshot_digest="snapshot-digest",
            violations=(AlignmentViolation("chapter", "warning", "review"),),
            repair_plan=("人工审阅候选",),
        )


class _RecordingGate:
    def __init__(self):
        self.calls = []

    @staticmethod
    def _candidate_digest(candidate):
        return "same-candidate"

    def build_snapshot(self, chapter_id, nodes, **kwargs):
        self.calls.append(("snapshot", chapter_id, kwargs))
        return {"chapter_id": chapter_id}

    async def evaluate(self, snapshot, candidate):
        self.calls.append(("evaluate", snapshot["chapter_id"], candidate))
        return AlignmentReport("pass", 1.0, "same-candidate", snapshot["chapter_id"])


class _LLM:
    calls = 0

    async def generate(self, *args, **kwargs):
        self.calls += 1
        raise AssertionError("prose LLM must not run")


@pytest.mark.asyncio
async def test_stream_returns_typed_gate_report_before_generation():
    llm = _LLM()
    workflow = AutoNovelGenerationWorkflow.__new__(AutoNovelGenerationWorkflow)
    workflow.hierarchy_gate = _Gate()
    workflow.story_node_repo = None
    workflow.memory_engine = None
    workflow.context_builder = None
    workflow.llm_service = llm

    events = [event async for event in workflow.generate_chapter_stream("novel-1", 1, "本章大纲")]
    assert events[0]["type"] == "error"
    assert events[0]["alignment_report"]["decision"] == "review"
    assert events[0]["alignment_report"]["candidate_digest"] == "candidate-digest"
    assert llm.calls == 0


@pytest.mark.asyncio
async def test_gate_cache_is_scoped_to_novel_chapter_and_snapshot():
    gate = _RecordingGate()
    evidence = {"layer3": "vector evidence"}

    first = await evaluate_chapter_candidate(
        gate,
        story_node_repo=None,
        novel_id="novel-a",
        chapter_number=1,
        candidate={"outline": "same"},
        context_evidence=evidence,
    )
    second = await evaluate_chapter_candidate(
        gate,
        story_node_repo=None,
        novel_id="novel-a",
        chapter_number=2,
        candidate={"outline": "same"},
        context_evidence=evidence,
    )

    assert first.snapshot_digest != second.snapshot_digest
    assert len([call for call in gate.calls if call[0] == "evaluate"]) == 2
    evaluate_calls = [call for call in gate.calls if call[0] == "evaluate"]
    assert evaluate_calls[0][2]["context_evidence"] == evidence
