from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from application.workflows.auto_novel_generation_workflow import (
    AutoNovelGenerationWorkflow,
)


class _CountingLLM:
    def __init__(self) -> None:
        self.generate_calls = 0
        self.stream_calls = 0

    @property
    def calls(self) -> int:
        return self.generate_calls + self.stream_calls

    async def generate(self, *_args, **_kwargs):
        self.generate_calls += 1
        return SimpleNamespace(content="生成内容")

    async def stream_generate(self, *_args, **_kwargs):
        self.stream_calls += 1
        yield "生成内容"


class _CountingContextBuilder:
    def __init__(self, memory_engine) -> None:
        self.build_calls = 0
        self.outline_context_calls = 0
        self.budget_allocator = SimpleNamespace(memory_engine=memory_engine)
        self.novel_repository = SimpleNamespace(
            get_by_id=lambda _novel_id: SimpleNamespace(
                target_words_per_chapter=2500,
                generation_prefs=SimpleNamespace(
                    inline_prose_aggregation_enabled=False,
                ),
            )
        )

    def build_structured_context(self, **_kwargs):
        self.build_calls += 1
        return {
            "layer1_text": "T0/T1",
            "layer2_text": "T2",
            "layer3_text": "T3",
            "token_usage": {"total": 12},
        }

    def build_context(self, **_kwargs) -> str:
        self.outline_context_calls += 1
        return "章节上下文"

    def build_voice_anchor_system_section(self, _novel_id: str) -> str:
        return ""


class _MemoryEngineWithoutFactLockSource:
    def __init__(self, llm_service: _CountingLLM) -> None:
        self.bible_repository = None
        self.llm_service = llm_service

    def build_fact_lock_section(self, _novel_id: str, _chapter_number: int) -> str:
        return ""

    def get_completed_beats_section(self, _novel_id: str) -> str:
        return ""

    def get_revealed_clues_section(self, _novel_id: str) -> str:
        return ""


def _workflow(*, memory_mode: str):
    llm_service = _CountingLLM()
    memory_engine = (
        None
        if memory_mode == "missing_engine"
        else _MemoryEngineWithoutFactLockSource(llm_service)
    )
    context_builder = _CountingContextBuilder(memory_engine)
    workflow = AutoNovelGenerationWorkflow(
        context_builder=context_builder,
        consistency_checker=object(),
        storyline_manager=object(),
        plot_arc_repository=object(),
        llm_service=llm_service,
        state_extractor=object(),
        memory_engine=memory_engine,
    )
    workflow._get_storyline_context = lambda *_args: ""
    workflow._get_plot_tension = lambda *_args: ""
    workflow._get_style_summary = lambda *_args: ""
    workflow.post_process_generated_chapter = AsyncMock(
        return_value={
            "style_warnings": [],
            "consistency_report": object(),
            "ghost_annotations": [],
        }
    )
    return workflow, context_builder, llm_service


def test_workflow_reuses_the_context_allocator_memory_engine():
    llm_service = _CountingLLM()
    memory_engine = SimpleNamespace(
        bible_repository=object(),
        llm_service=llm_service,
    )
    context_builder = _CountingContextBuilder(memory_engine)

    workflow = AutoNovelGenerationWorkflow(
        context_builder=context_builder,
        consistency_checker=object(),
        storyline_manager=object(),
        plot_arc_repository=object(),
        llm_service=llm_service,
        state_extractor=object(),
    )

    assert workflow.memory_engine is memory_engine
    assert workflow._required_narrative_memory_failure() == ""


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("memory_mode", "expected_reason"),
    [
        ("missing_engine", "required_narrative_memory_unavailable:memory_engine"),
        (
            "missing_fact_lock_source",
            "required_narrative_memory_unavailable:fact_lock_data_source",
        ),
    ],
)
async def test_generate_chapter_blocks_before_context_or_llm_when_required_memory_is_unavailable(
    memory_mode,
    expected_reason,
):
    workflow, context_builder, llm_service = _workflow(memory_mode=memory_mode)
    error = ""

    try:
        await workflow.generate_chapter("novel-1", 1, "本章大纲")
    except RuntimeError as exc:
        error = str(exc)

    assert llm_service.calls == 0
    assert context_builder.build_calls == 0
    assert error == expected_reason


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("memory_mode", "expected_reason"),
    [
        ("missing_engine", "required_narrative_memory_unavailable:memory_engine"),
        (
            "missing_fact_lock_source",
            "required_narrative_memory_unavailable:fact_lock_data_source",
        ),
    ],
)
async def test_generate_chapter_stream_blocks_before_context_or_llm_when_required_memory_is_unavailable(
    memory_mode,
    expected_reason,
):
    workflow, context_builder, llm_service = _workflow(memory_mode=memory_mode)

    events = [
        event
        async for event in workflow.generate_chapter_stream(
            "novel-1",
            1,
            "本章大纲",
        )
    ]

    assert llm_service.calls == 0
    assert context_builder.build_calls == 0
    assert events == [{"type": "error", "message": expected_reason}]


@pytest.mark.asyncio
async def test_suggest_outline_uses_its_established_seed_fallback_without_llm_when_memory_is_missing():
    workflow, context_builder, llm_service = _workflow(memory_mode="missing_engine")

    outline = await workflow.suggest_outline("novel-1", 3)

    assert outline.startswith("第3章：")
    assert context_builder.outline_context_calls == 0
    assert llm_service.calls == 0


@pytest.mark.asyncio
async def test_post_process_reports_memory_writeback_failure_instead_of_success():
    workflow, _context_builder, llm_service = _workflow(
        memory_mode="missing_fact_lock_source"
    )

    class _FailingMemoryEngine:
        def __init__(self, configured_llm_service) -> None:
            self.bible_repository = object()
            self.llm_service = configured_llm_service

        async def update_from_chapter(self, **_kwargs):
            raise RuntimeError("memory persistence unavailable")

    workflow.memory_engine = _FailingMemoryEngine(llm_service)
    workflow._scan_cliches = lambda _content: []
    workflow._extract_chapter_state = AsyncMock(return_value=object())
    workflow._check_consistency = lambda _state, _novel_id: object()
    workflow._detect_conflicts = lambda *_args: []
    workflow.post_process_generated_chapter = (
        AutoNovelGenerationWorkflow.post_process_generated_chapter.__get__(
            workflow,
            AutoNovelGenerationWorkflow,
        )
    )

    with pytest.raises(
        RuntimeError,
        match="required_narrative_memory_writeback_failed:memory persistence unavailable",
    ):
        await workflow.post_process_generated_chapter(
            "novel-1",
            4,
            "本章大纲",
            "本章正文",
        )
