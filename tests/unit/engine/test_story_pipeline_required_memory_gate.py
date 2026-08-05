from types import SimpleNamespace

import pytest

from engine.pipeline.base import BaseStoryPipeline
from engine.pipeline.context import PipelineContext
from engine.pipeline.steps import StepResult


class _Pipeline(BaseStoryPipeline):
    async def _step_find_next_chapter(self, _ctx: PipelineContext) -> StepResult:
        return StepResult.ok()

    async def _step_prepare_governance(self, _ctx: PipelineContext) -> StepResult:
        return StepResult.skip_step()


class _CountingLLM:
    def __init__(self) -> None:
        self.calls = 0

    async def generate(self, *_args, **_kwargs):
        self.calls += 1
        return SimpleNamespace(content="unused")


class _LLMBackedPreplanningService:
    def __init__(self, llm_service: _CountingLLM) -> None:
        self._llm_service = llm_service

    async def ensure_execution_plan(self, **_kwargs) -> str:
        await self._llm_service.generate("preplanning prompt", object())
        return "章前执行剧本"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("memory_engine", "aftermath_pipeline", "missing_dependency"),
    [
        (
            None,
            SimpleNamespace(_memory_engine=None),
            "memory_engine",
        ),
        (
            SimpleNamespace(bible_repository=None, llm_service=object()),
            None,
            "fact_lock_data_source",
        ),
        (
            SimpleNamespace(bible_repository=object(), llm_service=object()),
            None,
            "shared_memory_engine",
        ),
    ],
)
async def test_long_form_missing_memory_stops_before_chapter_preplanning_llm(
    memory_engine,
    aftermath_pipeline,
    missing_dependency,
):
    if aftermath_pipeline is None:
        if missing_dependency == "fact_lock_data_source":
            aftermath_pipeline = SimpleNamespace(_memory_engine=memory_engine)
        else:
            aftermath_pipeline = SimpleNamespace(_memory_engine=object())

    llm_service = _CountingLLM()
    context = PipelineContext(
        novel_id="novel-required-memory",
        chapter_number=2,
        outline="仅有轻量章节提纲",
    )
    context.llm_service = llm_service
    context.context_builder = SimpleNamespace(
        budget_allocator=SimpleNamespace(memory_engine=memory_engine)
    )
    context.chapter_repository = SimpleNamespace(db=object())
    context.aftermath_pipeline = aftermath_pipeline
    context.chapter_preplanning_service = _LLMBackedPreplanningService(llm_service)
    context.metadata["requires_narrative_memory"] = True

    result = await _Pipeline().run_chapter(context)

    assert result.success is False
    assert result.error == f"required_narrative_memory_unavailable:{missing_dependency}"
    assert llm_service.calls == 0
