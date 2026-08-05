from __future__ import annotations

import hashlib
import sqlite3
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from engine.pipeline.base import BaseStoryPipeline
from engine.pipeline.context import PipelineContext
from engine.pipeline.steps import StepResult
from engine.pipeline.prose_composer import ChapterProseInvocationComposer, ProseCompositionRequest, ProseCompositionResult
from application.engine.services.context_budget_allocator import ContextBudgetAllocator
from application.engine.services.context_budget_models import FactLockUnavailableError
from domain.novel.entities.chapter import ChapterStatus
from domain.novel.value_objects.novel_id import NovelId


class _Pipeline(BaseStoryPipeline):
    pass


class _Composer:
    def __init__(self, result: ProseCompositionResult):
        self.result = result
        self.requests = []

    async def compose(self, request):
        self.requests.append(request)
        if request.stream_sink:
            request.stream_sink(self.result.content)
        return self.result


class _CountingLLM:
    def __init__(self):
        self.calls = 0

    async def generate(self, *args, **kwargs):
        self.calls += 1
        return SimpleNamespace(content="不应调用")


def _required_memory_context(*, memory_engine, aftermath_pipeline):
    context = PipelineContext(
        novel_id="novel-required-memory",
        chapter_number=2,
        outline="本章大纲",
    )
    context.llm_service = _CountingLLM()
    context.context_builder = SimpleNamespace(
        budget_allocator=SimpleNamespace(memory_engine=memory_engine)
    )
    context.chapter_repository = SimpleNamespace(db=object())
    context.aftermath_pipeline = aftermath_pipeline
    context.metadata["requires_narrative_memory"] = True
    return context


@pytest.mark.asyncio
async def test_long_form_context_stops_before_llm_when_memory_engine_is_missing():
    context = _required_memory_context(
        memory_engine=None,
        aftermath_pipeline=SimpleNamespace(_memory_engine=None),
    )

    result = await _Pipeline()._step_build_context(context)

    assert not result.passed
    assert "memory_engine" in result.message
    assert context.llm_service.calls == 0


@pytest.mark.asyncio
async def test_long_form_context_stops_before_llm_when_fact_lock_source_is_missing():
    memory_engine = SimpleNamespace(bible_repository=None, llm_service=object())
    context = _required_memory_context(
        memory_engine=memory_engine,
        aftermath_pipeline=SimpleNamespace(_memory_engine=memory_engine),
    )

    result = await _Pipeline()._step_build_context(context)

    assert not result.passed
    assert "fact_lock_data_source" in result.message
    assert context.llm_service.calls == 0


@pytest.mark.asyncio
async def test_long_form_context_stops_before_llm_when_aftermath_pipeline_is_missing():
    memory_engine = SimpleNamespace(bible_repository=object(), llm_service=object())
    context = _required_memory_context(
        memory_engine=memory_engine,
        aftermath_pipeline=None,
    )

    result = await _Pipeline()._step_build_context(context)

    assert not result.passed
    assert "aftermath_pipeline" in result.message
    assert context.llm_service.calls == 0


@pytest.mark.asyncio
async def test_long_form_context_does_not_fallback_after_required_builder_failure():
    memory_engine = SimpleNamespace(bible_repository=object(), llm_service=object())
    fallback_used = False

    class Workflow:
        context_builder = SimpleNamespace(
            budget_allocator=SimpleNamespace(memory_engine=memory_engine)
        )

        def prepare_chapter_generation(self, *args, **kwargs):
            raise RuntimeError("context backend unavailable")

    class FallbackBuilder:
        budget_allocator = SimpleNamespace(memory_engine=memory_engine)

        def build_context(self, **kwargs):
            nonlocal fallback_used
            fallback_used = True
            return "weak fallback"

    context = _required_memory_context(
        memory_engine=memory_engine,
        aftermath_pipeline=SimpleNamespace(_memory_engine=memory_engine),
    )
    context.chapter_workflow = Workflow()
    context.context_builder = FallbackBuilder()

    result = await _Pipeline()._step_build_context(context)

    assert not result.passed
    assert "context backend unavailable" in result.message
    assert fallback_used is False
    assert context.llm_service.calls == 0


@pytest.mark.asyncio
async def test_story_pipeline_carries_workflow_context_budget_to_prose_metadata():
    chapter_workflow = SimpleNamespace(
        prepare_chapter_generation=lambda *args, **kwargs: {
            "context": "已预算主上下文",
            "context_tokens": 80,
            "context_budget_tokens": 123,
            "voice_anchors": "",
        }
    )
    ctx = PipelineContext(
        novel_id="novel-1",
        chapter_number=2,
        outline="本章大纲",
    )
    ctx.chapter_workflow = chapter_workflow

    result = await _Pipeline()._step_build_context(ctx)

    assert result.passed
    assert ctx.context_text == "已预算主上下文"
    assert ctx.metadata["context_budget_tokens"] == 123


@pytest.mark.asyncio
async def test_story_pipeline_drains_auxiliary_stages_before_building_context():
    events = []

    class Aftermath:
        async def drain_auxiliary_stages(self):
            events.append("drain")

    class ChapterWorkflow:
        def prepare_chapter_generation(self, *args, **kwargs):
            events.append("build")
            return {
                "context": "最新演进状态",
                "context_tokens": 4,
                "context_budget_tokens": 100,
                "voice_anchors": "",
            }

    ctx = PipelineContext(novel_id="novel-1", chapter_number=2, outline="本章大纲")
    ctx.aftermath_pipeline = Aftermath()
    ctx.chapter_workflow = ChapterWorkflow()

    result = await _Pipeline()._step_build_context(ctx)

    assert result.passed
    assert events == ["drain", "build"]


@pytest.mark.asyncio
async def test_story_pipeline_does_not_fallback_after_configured_fact_lock_failure(monkeypatch):
    class BrokenMemoryEngine:
        def build_fact_lock_section(self, novel_id, chapter_number):
            raise RuntimeError("configured fact lock unavailable")

    allocator = ContextBudgetAllocator(memory_engine=BrokenMemoryEngine())
    monkeypatch.setattr(allocator, "_estimate_total_chapters", lambda _novel_id: 100)

    class ChapterWorkflow:
        def prepare_chapter_generation(self, *args, **kwargs):
            allocator.allocate("novel-1", 2, "outline", total_budget=1000)

    class FallbackBuilder:
        used = False

        def build_context(self, **kwargs):
            self.used = True
            return "fallback without facts"

        def build_voice_anchor_system_section(self, novel_id):
            return ""

    fallback_builder = FallbackBuilder()
    ctx = PipelineContext(novel_id="novel-1", chapter_number=2, outline="本章大纲")
    ctx.chapter_workflow = ChapterWorkflow()
    ctx.context_builder = fallback_builder

    result = await _Pipeline()._step_build_context(ctx)

    assert not result.passed
    assert "configured fact lock unavailable" in result.message
    assert fallback_builder.used is False


@pytest.mark.asyncio
async def test_story_pipeline_preserves_generic_context_builder_fallback():
    class ChapterWorkflow:
        def prepare_chapter_generation(self, *args, **kwargs):
            raise ValueError("recoverable planning failure")

    class FallbackBuilder:
        def build_context(self, **kwargs):
            return "fallback context"

        def build_voice_anchor_system_section(self, novel_id):
            return ""

    ctx = PipelineContext(novel_id="novel-1", chapter_number=2, outline="本章大纲")
    ctx.chapter_workflow = ChapterWorkflow()
    ctx.context_builder = FallbackBuilder()

    result = await _Pipeline()._step_build_context(ctx)

    assert result.passed
    assert ctx.context_text == "fallback context"
    assert ctx.metadata["context_budget_tokens"] == 20000


@pytest.mark.asyncio
@pytest.mark.parametrize("primary_failure", [None, ValueError("recoverable planning failure")])
async def test_story_pipeline_rejects_fact_lock_failure_from_context_builder(primary_failure):
    class ChapterWorkflow:
        def prepare_chapter_generation(self, *args, **kwargs):
            raise primary_failure

    class BrokenContextBuilder:
        def build_context(self, **kwargs):
            raise FactLockUnavailableError("fallback fact lock unavailable")

        def build_voice_anchor_system_section(self, novel_id):
            return ""

    ctx = PipelineContext(novel_id="novel-1", chapter_number=2, outline="本章大纲")
    if primary_failure is not None:
        ctx.chapter_workflow = ChapterWorkflow()
    ctx.context_builder = BrokenContextBuilder()

    result = await _Pipeline()._step_build_context(ctx)

    assert not result.passed
    assert "fallback fact lock unavailable" in result.message
    assert ctx.context_text == ""


def _budgeted_chapter_workflow(context: str, budget: int):
    return SimpleNamespace(
        prepare_chapter_generation=lambda *args, **kwargs: {
            "context": context,
            "context_tokens": ContextBudgetAllocator().estimate_tokens(context),
            "context_budget_tokens": budget,
            "voice_anchors": "",
        }
    )


@pytest.mark.asyncio
async def test_story_pipeline_rejects_governance_that_cannot_fit_context_budget():
    base_context = "b" * 360  # 90 tokens
    ctx = PipelineContext(novel_id="novel-1", chapter_number=2, outline="本章大纲")
    ctx.chapter_workflow = _budgeted_chapter_workflow(base_context, 100)
    ctx.governance_budget = {
        "max_new_storylines": 1,
        "max_debt_closures": 1,
        "allowed_reveal_level": "hint",
        "notes": ["治理约束" * 100],
    }

    result = await _Pipeline()._step_build_context(ctx)

    assert not result.passed
    assert "context budget" in result.message
    assert ContextBudgetAllocator().estimate_tokens(ctx.context_text) <= 100


@pytest.mark.asyncio
async def test_story_pipeline_rejects_due_foreshadowing_that_cannot_fit_context_budget():
    entry = SimpleNamespace(
        status="pending",
        suggested_resolve_chapter=2,
        importance="critical",
        question="钟楼暗门必须推进" * 100,
        chapter=1,
    )
    registry = SimpleNamespace(subtext_entries=[entry])
    ctx = PipelineContext(novel_id="novel-1", chapter_number=2, outline="本章大纲")
    ctx.chapter_workflow = _budgeted_chapter_workflow("b" * 360, 100)
    ctx.foreshadowing_repository = SimpleNamespace(
        get_by_novel_id=lambda novel_id: registry
    )

    result = await _Pipeline()._step_build_context(ctx)

    assert not result.passed
    assert "context budget" in result.message
    assert ContextBudgetAllocator().estimate_tokens(ctx.context_text) <= 100


@pytest.mark.asyncio
async def test_story_pipeline_emits_governance_and_due_foreshadowing_once_within_budget():
    entry = SimpleNamespace(
        status="pending",
        suggested_resolve_chapter=2,
        importance="critical",
        question="钟楼暗门必须推进",
        chapter=1,
    )
    ctx = PipelineContext(novel_id="novel-1", chapter_number=2, outline="本章大纲")
    ctx.chapter_workflow = _budgeted_chapter_workflow("主上下文", 1000)
    ctx.governance_budget = {
        "max_new_storylines": 1,
        "max_debt_closures": 1,
        "allowed_reveal_level": "hint",
        "notes": ["保持事实一致"],
    }
    ctx.foreshadowing_repository = SimpleNamespace(
        get_by_novel_id=lambda novel_id: SimpleNamespace(subtext_entries=[entry])
    )

    result = await _Pipeline()._step_build_context(ctx)

    assert result.passed
    assert ctx.context_text.count("=== 本章叙事治理预算 ===") == 1
    assert ctx.context_text.count("=== 本章应推进的伏笔 ===") == 1
    assert "钟楼暗门必须推进" in ctx.context_text
    assert ContextBudgetAllocator().estimate_tokens(ctx.context_text) <= 1000


def test_story_pipeline_prose_fallback_uses_full_continuity_ledger():
    previous_chapter = SimpleNamespace(
        number=1,
        title="第1章",
        outline="沈青准备潜入钟楼。",
        content="沈青目睹林澈死亡，并确认赤铜钥匙归自己保管。",
    )
    current_node = SimpleNamespace(
        number=2,
        title="第2章",
        node_type=SimpleNamespace(value="chapter"),
        metadata={},
    )
    previous_node = SimpleNamespace(
        number=1,
        title="第1章",
        node_type=SimpleNamespace(value="chapter"),
        metadata={},
        outline=previous_chapter.outline,
        content=previous_chapter.content,
    )
    chapter_repository = SimpleNamespace(
        list_by_novel=lambda _novel_id: [previous_chapter]
    )
    story_node_repo = SimpleNamespace(
        get_tree_sync=lambda _novel_id: SimpleNamespace(nodes=[previous_node, current_node])
    )
    ctx = PipelineContext(
        novel_id="novel-1",
        chapter_number=2,
        chapter_node=current_node,
    )
    ctx.chapter_repository = chapter_repository
    ctx.story_node_repo = story_node_repo

    _Pipeline()._attach_chapter_preplan_metadata(ctx)

    assert "林澈死亡" in ctx.metadata["continuity_context"]
    assert "赤铜钥匙" in ctx.metadata["continuity_context"]


def test_chapter_prose_composer_keeps_full_context_before_additional_continuity():
    composer = ChapterProseInvocationComposer()
    request = ProseCompositionRequest(
        novel_id="novel-1",
        chapter_number=4,
        outline="七段细纲",
        context_text="T0 世界观事实\nT1 有效摘要\nT2 最近承接\nT3 检索证据",
        target_words=2000,
        metadata={
            "key_plot_points": ["情节点1", "情节点2"],
            "chapter_characters": ["林渊", "林晚"],
            "chapter_plan_json": {"unused": True},
            "previous_summary": "不应进入 prompt",
            "previous_ending": "不应进入 prompt",
            "continuity_context": "章前规划补充",
        },
    )

    variables = composer._build_variables(request)

    assert variables == {
        "target_words": 2000,
        "chapter_outline": "七段细纲",
        "continuity_context": (
            "T0 世界观事实\nT1 有效摘要\nT2 最近承接\nT3 检索证据"
            "\n\n=== ADDITIONAL CONTINUITY ===\n章前规划补充"
        ),
    }


def test_chapter_prose_composer_does_not_duplicate_continuity_already_in_full_context():
    variables = ChapterProseInvocationComposer()._build_variables(
        ProseCompositionRequest(
            novel_id="novel-1",
            chapter_number=4,
            outline="七段细纲",
            context_text="事实锁\n章前规划补充\n检索证据",
            metadata={"continuity_context": "章前规划补充"},
        )
    )

    assert variables["continuity_context"] == "事实锁\n章前规划补充\n检索证据"


def test_chapter_prose_composer_compresses_additional_continuity_inside_context_budget():
    allocator = ContextBudgetAllocator()
    base_context = "b" * 320  # 80 tokens
    additional = "c" * 400  # 100 tokens before its header

    variables = ChapterProseInvocationComposer()._build_variables(
        ProseCompositionRequest(
            novel_id="novel-1",
            chapter_number=4,
            outline="七段细纲",
            context_text=base_context,
            metadata={
                "continuity_context": additional,
                "context_budget_tokens": 100,
            },
        )
    )

    emitted = variables["continuity_context"]
    assert emitted.startswith(base_context + "\n\n=== ADDITIONAL CONTINUITY ===\n")
    assert additional not in emitted
    assert allocator.estimate_tokens(emitted) <= 100


def test_chapter_prose_composer_drops_additional_continuity_when_main_context_fills_budget():
    allocator = ContextBudgetAllocator()
    base_context = "b" * 400  # 100 tokens

    variables = ChapterProseInvocationComposer()._build_variables(
        ProseCompositionRequest(
            novel_id="novel-1",
            chapter_number=4,
            outline="七段细纲",
            context_text=base_context,
            metadata={
                "continuity_context": "章前规划补充",
                "context_budget_tokens": 100,
            },
        )
    )

    assert variables["continuity_context"] == base_context
    assert allocator.estimate_tokens(variables["continuity_context"]) == 100


@pytest.mark.asyncio
async def test_chapter_prose_composer_reuses_committed_story_pipeline_content(monkeypatch):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript(
        """
        CREATE TABLE ai_invocation_sessions (
            id TEXT PRIMARY KEY,
            operation TEXT,
            status TEXT,
            context_json TEXT DEFAULT '{}',
            metadata_json TEXT DEFAULT '{}',
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE ai_adoption_decisions (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            attempt_id TEXT DEFAULT '',
            accepted_content TEXT DEFAULT '',
            accepted_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE ai_adoption_commits (
            id TEXT PRIMARY KEY,
            session_id TEXT NOT NULL,
            decision_id TEXT NOT NULL,
            status TEXT DEFAULT 'pending',
            updated_at TEXT DEFAULT CURRENT_TIMESTAMP
        );
        INSERT INTO ai_invocation_sessions
            (id, operation, status, context_json, metadata_json)
        VALUES
            (
                'session-1',
                'autopilot.chapter.prose',
                'completed',
                '{"novel_id":"novel-1","chapter_number":2}',
                '{"commit_owner":"story_pipeline_save_step"}'
            );
        INSERT INTO ai_adoption_decisions
            (id, session_id, accepted_content)
        VALUES ('decision-1', 'session-1', '已采纳正文');
        INSERT INTO ai_adoption_commits
            (id, session_id, decision_id, status)
        VALUES ('commit-1', 'session-1', 'decision-1', 'succeeded');
        """
    )

    class _Db:
        def fetch_one(self, sql, params=()):
            row = conn.execute(sql, params).fetchone()
            return dict(row) if row else None

    import infrastructure.persistence.database.connection

    monkeypatch.setattr(infrastructure.persistence.database.connection, "get_database", lambda *_args, **_kwargs: _Db())
    chunks = []
    request = ProseCompositionRequest(
        novel_id="novel-1",
        chapter_number=2,
        stream_sink=chunks.append,
    )

    result = await ChapterProseInvocationComposer().compose(request)

    assert result.content == "已采纳正文"
    assert result.status == "committed_story_pipeline_content"
    assert chunks == ["已采纳正文"]


@pytest.mark.asyncio
async def test_story_pipeline_uses_chapter_prose_composer_for_auto_approved_flow():
    composer = _Composer(ProseCompositionResult(content="整章正文"))
    pipeline = _Pipeline()
    ctx = PipelineContext(
        novel_id="novel-composer",
        chapter_number=3,
        outline="本章大纲",
        context_text="世界观上下文",
        target_word_count=2400,
        auto_approve_mode=True,
    )
    ctx.llm_service = object()
    ctx.prose_composer = composer

    result = await pipeline._step_generate(ctx)

    assert result.passed
    assert ctx.chapter_content == "整章正文"
    assert ctx.word_count == len("整章正文")
    assert composer.requests[0].outline == "本章大纲"
    assert composer.requests[0].context_text == "世界观上下文"


@pytest.mark.asyncio
async def test_story_pipeline_save_falls_back_when_queue_write_not_visible(monkeypatch):
    pipeline = _Pipeline()
    ctx = PipelineContext(
        novel_id="novel-save",
        chapter_number=1,
        chapter_content="正文",
        word_count=2,
    )
    ctx.chapter_repository = SimpleNamespace(
        get_by_novel_and_number=lambda *_args: None,
    )
    saved = []

    async def _save_via_repo(_ctx):
        saved.append((_ctx.novel_id, _ctx.chapter_number, _ctx.chapter_content))

    monkeypatch.setattr(
        pipeline,
        "_prepare_chapter_persistence_receipt",
        lambda _ctx: None,
    )
    monkeypatch.setattr(pipeline, "_push_persistence_command", lambda _ctx: True)
    monkeypatch.setattr(pipeline, "_wait_for_chapter_persistence", lambda _ctx: None)
    receipts = iter([False, True])
    monkeypatch.setattr(
        pipeline,
        "_chapter_completed_in_repository",
        lambda _ctx: next(receipts),
    )
    monkeypatch.setattr(pipeline, "_save_chapter_via_repository", _save_via_repo)

    result = await pipeline._step_save_chapter(ctx)

    assert result.passed
    assert saved == [("novel-save", 1, "正文")]
    assert ctx.chapter_saved is True
    assert ctx.save_method == "queue"


@pytest.mark.asyncio
async def test_story_pipeline_queue_idle_without_durable_receipt_is_not_saved(monkeypatch):
    pipeline = _Pipeline()
    ctx = PipelineContext(
        novel_id="novel-save",
        chapter_number=1,
        chapter_content="正文",
        word_count=2,
    )
    ctx.chapter_repository = SimpleNamespace(
        get_by_novel_and_number=lambda *_args: None,
    )

    monkeypatch.setattr(
        pipeline,
        "_prepare_chapter_persistence_receipt",
        lambda _ctx: None,
    )
    monkeypatch.setattr(pipeline, "_push_persistence_command", lambda _ctx: True)
    monkeypatch.setattr(pipeline, "_wait_for_chapter_persistence", lambda _ctx: None)
    monkeypatch.setattr(pipeline, "_chapter_completed_in_repository", lambda _ctx: False)
    monkeypatch.setattr(
        pipeline,
        "_save_chapter_via_repository",
        AsyncMock(return_value=None),
    )

    result = await pipeline._step_save_chapter(ctx)

    assert not result.passed
    assert "matching_chapter_receipt_unavailable" in result.message
    assert ctx.chapter_saved is False


def test_story_pipeline_accepts_only_matching_durable_chapter_receipt():
    content = "正文"
    content_sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()
    ctx = PipelineContext(
        novel_id="novel-save",
        chapter_number=1,
        chapter_content=content,
    )
    persisted = SimpleNamespace(
        novel_id=NovelId("other-novel"),
        number=1,
        status=ChapterStatus.COMPLETED,
        content=content,
        content_sha256=content_sha256,
        content_revision=1,
    )
    ctx.chapter_repository = SimpleNamespace(
        get_by_novel_and_number=lambda *_args: persisted
    )
    ctx.metadata["chapter_persistence_receipt"] = {
        "novel_id": "novel-save",
        "chapter_number": 1,
        "content_sha256": content_sha256,
        "content_revision": 2,
    }
    pipeline = _Pipeline()

    assert pipeline._chapter_completed_in_repository(ctx) is False

    persisted.novel_id = NovelId("novel-save")
    persisted.content_revision = 3
    assert pipeline._chapter_completed_in_repository(ctx) is False

    persisted.content_revision = 2
    assert pipeline._chapter_completed_in_repository(ctx) is True


@pytest.mark.asyncio
async def test_story_pipeline_post_commit_fails_closed_without_canonical_readiness():
    class _Aftermath:
        async def run_after_chapter_saved(self, *args, **kwargs):
            return {}

    pipeline = _Pipeline()
    ctx = PipelineContext(
        novel_id="novel-canonical-failure",
        chapter_number=1,
        chapter_content="正文",
        word_count=2,
    )
    ctx.chapter_repository = object()
    ctx.aftermath_pipeline = _Aftermath()

    result = await pipeline._step_run_post_commit(ctx)

    assert not result.passed
    assert result.message == "canonical_aftermath_not_ready"
    assert ctx.narrative_sync_ok is False


@pytest.mark.asyncio
async def test_story_pipeline_post_commit_passes_persisted_content_version():
    class _Aftermath:
        def __init__(self):
            self.kwargs = None

        async def run_after_chapter_saved(self, *args, **kwargs):
            self.kwargs = kwargs
            return {"narrative_sync_ok": True}

    content = "正文"
    aftermath = _Aftermath()
    pipeline = _Pipeline()
    pipeline._is_chapter_narrative_ready = lambda _ctx: True
    ctx = PipelineContext(
        novel_id="novel-versioned-aftermath",
        chapter_number=1,
        chapter_content=content,
        word_count=2,
    )
    ctx.aftermath_pipeline = aftermath
    ctx.metadata["chapter_persistence_receipt"] = {
        "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        "content_revision": 4,
    }

    result = await pipeline._step_run_post_commit(ctx)

    assert result.passed
    assert aftermath.kwargs["expected_content_sha256"] == hashlib.sha256(
        content.encode("utf-8")
    ).hexdigest()
    assert aftermath.kwargs["expected_content_revision"] == 4


@pytest.mark.asyncio
async def test_story_pipeline_post_commit_blocks_when_memory_state_write_failed():
    class _Aftermath:
        async def run_after_chapter_saved(self, *args, **kwargs):
            return {
                "narrative_sync_ok": False,
                "memory_engine_ok": False,
                "failure_reason": "memory_engine_update_failed",
            }

    pipeline = _Pipeline()
    pipeline._is_chapter_narrative_ready = lambda _ctx: True
    ctx = PipelineContext(
        novel_id="novel-memory-write-failure",
        chapter_number=1,
        chapter_content="正文",
        word_count=2,
    )
    ctx.aftermath_pipeline = _Aftermath()

    result = await pipeline._step_run_post_commit(ctx)

    assert not result.passed
    assert result.message == "canonical_aftermath_not_ready"
    assert ctx.narrative_sync_ok is False


@pytest.mark.asyncio
async def test_story_pipeline_stops_before_governance_when_auxiliary_sync_fails(monkeypatch):
    class FailingAftermath:
        async def drain_auxiliary_stages(self):
            raise RuntimeError("evolution state unavailable")

    pipeline = _Pipeline()
    find_next = AsyncMock(return_value=StepResult.ok())
    prepare_governance = AsyncMock(return_value=StepResult.ok())
    prepare_plan = AsyncMock(return_value=StepResult.fail("should not reach planning"))
    monkeypatch.setattr(pipeline, "_step_find_next_chapter", find_next)
    monkeypatch.setattr(pipeline, "_step_prepare_governance", prepare_governance)
    monkeypatch.setattr(pipeline, "_step_prepare_chapter_plan", prepare_plan)

    ctx = PipelineContext(novel_id="novel-1", chapter_number=2, outline="本章大纲")
    ctx.aftermath_pipeline = FailingAftermath()

    result = await pipeline.run_chapter(ctx)

    assert result.success is False
    assert result.error == "required_auxiliary_state_sync_failed:evolution state unavailable"
    prepare_governance.assert_not_awaited()
    prepare_plan.assert_not_awaited()


@pytest.mark.asyncio
async def test_story_pipeline_stops_when_governance_rejects_continuity(monkeypatch):
    pipeline = _Pipeline()
    find_next = AsyncMock(return_value=StepResult.ok())
    prepare_governance = AsyncMock(
        return_value=StepResult.fail("写前连续性检查未通过：需要暂停")
    )
    prepare_plan = AsyncMock(return_value=StepResult.fail("should not reach planning"))
    monkeypatch.setattr(pipeline, "_step_find_next_chapter", find_next)
    monkeypatch.setattr(pipeline, "_step_prepare_governance", prepare_governance)
    monkeypatch.setattr(pipeline, "_step_prepare_chapter_plan", prepare_plan)

    result = await pipeline.run_chapter(PipelineContext(novel_id="novel-1"))

    assert result.success is False
    assert result.error == "写前连续性检查未通过：需要暂停"
    prepare_plan.assert_not_awaited()


@pytest.mark.asyncio
async def test_story_pipeline_does_not_finalize_after_canonical_failure(monkeypatch):
    pipeline = _Pipeline()
    ok_steps = (
        "_step_find_next_chapter",
        "_step_prepare_governance",
        "_step_prepare_chapter_plan",
        "_step_build_context",
        "_step_generate",
        "_step_validate_content",
        "_step_save_chapter",
        "_step_validate_voice",
        "_step_score_tension",
    )
    for step_name in ok_steps:
        monkeypatch.setattr(
            pipeline,
            step_name,
            AsyncMock(return_value=StepResult.ok()),
        )
    monkeypatch.setattr(
        pipeline,
        "_step_run_post_commit",
        AsyncMock(return_value=StepResult.fail("canonical_aftermath_not_ready")),
    )
    finalize = AsyncMock(return_value=StepResult.ok())
    monkeypatch.setattr(pipeline, "_step_finalize", finalize)

    result = await pipeline.run_chapter(PipelineContext(novel_id="novel-1"))

    assert result.success is False
    assert result.error == "canonical_aftermath_not_ready"
    finalize.assert_not_awaited()


@pytest.mark.asyncio
async def test_story_pipeline_marks_awaiting_review_from_composer():
    composer = _Composer(ProseCompositionResult(awaiting_review=True, session_id="session-1"))
    pipeline = _Pipeline()
    ctx = PipelineContext(
        novel_id="novel-review",
        chapter_number=1,
        outline="本章大纲",
        auto_approve_mode=True,
    )
    ctx.llm_service = object()
    ctx.prose_composer = composer

    result = await pipeline._step_generate(ctx)

    assert not result.passed
    assert result.message == "awaiting_ai_review"
    assert ctx.metadata["awaiting_ai_review"] is True
    assert ctx.metadata["active_invocation_session_id"] == "session-1"
