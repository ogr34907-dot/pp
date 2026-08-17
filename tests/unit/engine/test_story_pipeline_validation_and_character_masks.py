from __future__ import annotations

from types import SimpleNamespace

import pytest

from engine.pipeline.base import BaseStoryPipeline
from engine.pipeline.context import PipelineContext, PipelineResult
from engine.pipeline.steps import StepResult
from engine.pipelines.themed_pipeline import ThemedStoryPipeline
from engine.pipelines.wuxia_pipeline import WuxiaPipeline
from engine.runtime.policy_validator import (
    ADVISORY_FAIL,
    HARD_FAIL,
    PASS,
    UNEVALUATED,
    PolicyReport,
)


class _Pipeline(BaseStoryPipeline):
    pass


class _Slot:
    def __init__(self, character_id: str, name: str):
        self.character_id = character_id
        self.name = name


class _Character:
    def __init__(self, character_id: str, name: str):
        self.character_id = SimpleNamespace(value=character_id)
        self.name = name
        self.compute_calls: list[int] = []

    def compute_mask(self, *, up_to_chapter: int):
        self.compute_calls.append(up_to_chapter)
        return {
            "name": self.name,
            "core_belief": "先活下来",
            "moral_taboos": ["不伤无辜"],
            "voice_profile": {"style": "惜字如金", "sentence_pattern": "短句"},
            "active_wounds": [],
        }


class _Kernel:
    def __init__(self, character: _Character, slots=None):
        self.character = character
        self.slots = list(slots or [])
        self.plan_calls = 0

    def get_cast_slots(self, novel_id, chapter_number):
        return list(self.slots)

    def plan_cast(self, novel_id, chapter_number, outline):
        self.plan_calls += 1
        return SimpleNamespace(slots=[_Slot("char-1", self.character.name)])

    def _get_bible(self, novel_id):
        return SimpleNamespace(characters=[self.character])

    def _char_id(self, character):
        return character.character_id.value


def _report(status: str, score: float | None, violations=None) -> PolicyReport:
    return PolicyReport(
        overall_score=score,
        dimensions={
            "language_style": 0.9,
            "character_consistency": 0.9,
            "plot_density": 0.9,
            "naming": 0.9,
            "viewpoint": 0.9,
            "rhythm": 0.9,
        },
        violations=list(violations or []),
        status=status,
    )


def _validation_context(report=None) -> PipelineContext:
    ctx = PipelineContext(
        novel_id="novel-audit",
        chapter_number=4,
        outline="本章让主角发现线索",
        chapter_content="林风走进雨幕。",
    )
    if report is not None:
        ctx.policy_validator = SimpleNamespace(advise=lambda **kwargs: report)
    return ctx


def test_story_pipeline_projects_masks_from_existing_cast_and_bible_kernel():
    character = _Character("char-1", "林风")
    kernel = _Kernel(character, slots=[_Slot("char-1", "林风")])
    ctx = _validation_context()
    ctx.context_builder = SimpleNamespace(
        budget_allocator=SimpleNamespace(character_narrative_kernel=kernel)
    )

    masks = _Pipeline()._get_character_masks(ctx)

    assert list(masks) == ["char-1"]
    assert masks["char-1"].name == "林风"
    assert masks["char-1"].core_belief == "先活下来"
    assert masks["char-1"].chapter_number == 4
    assert character.compute_calls == [4]
    assert _Pipeline()._get_character_names(ctx) == ["林风"]
    assert kernel.plan_calls == 0


def test_story_pipeline_plans_cast_when_no_persisted_slots_exist():
    character = _Character("char-1", "林风")
    kernel = _Kernel(character)
    ctx = _validation_context()
    ctx.context_builder = SimpleNamespace(
        budget_allocator=SimpleNamespace(character_narrative_kernel=kernel)
    )

    masks = _Pipeline()._get_character_masks(ctx)

    assert set(masks) == {"char-1"}
    assert kernel.plan_calls == 1


@pytest.mark.asyncio
async def test_policy_validation_pass_advisory_and_unevaluated_states_are_explicit():
    pipeline = _Pipeline()

    passed_ctx = _validation_context(_report(PASS, 0.91))
    passed = await pipeline._step_validate_content(passed_ctx)
    assert passed.passed is True
    assert passed_ctx.validation_status == PASS
    assert passed_ctx.validation_passed is True
    assert passed_ctx.validation_score == 0.91

    advisory_ctx = _validation_context(
        _report(ADVISORY_FAIL, 0.58, [{"type": "style", "severity": 0.5}])
    )
    advisory = await pipeline._step_validate_content(advisory_ctx)
    assert advisory.passed is True
    assert advisory_ctx.validation_status == ADVISORY_FAIL
    assert advisory_ctx.validation_passed is False

    unevaluated_ctx = _validation_context()
    unevaluated = await pipeline._step_validate_content(unevaluated_ctx)
    assert unevaluated.passed is True
    assert unevaluated_ctx.validation_status == UNEVALUATED
    assert unevaluated_ctx.validation_score is None
    assert unevaluated_ctx.validation_passed is False


class _ValidationRunPipeline(_Pipeline):
    def __init__(self):
        super().__init__()
        self.save_attempted = False

    async def _step_find_next_chapter(self, ctx):
        return StepResult.ok()

    async def _ensure_auxiliary_stages_drained(self, ctx):
        return StepResult.ok()

    async def _step_prepare_governance(self, ctx):
        return StepResult.ok()

    async def _step_prepare_chapter_plan(self, ctx):
        return StepResult.ok()

    async def _step_build_context(self, ctx):
        return StepResult.ok()

    async def _step_generate(self, ctx):
        ctx.chapter_content = "林风走进雨幕。"
        ctx.word_count = len(ctx.chapter_content)
        return StepResult.ok()

    async def _step_save_chapter(self, ctx):
        self.save_attempted = True
        return StepResult.ok()

    async def _step_validate_voice(self, ctx):
        return StepResult.ok()

    async def _step_run_post_commit(self, ctx):
        return StepResult.ok()

    async def _step_score_tension(self, ctx):
        return StepResult.ok()

    async def _step_finalize(self, ctx):
        return StepResult.ok()

    def _novel_stream_should_stop(self, novel_id):
        return False


@pytest.mark.asyncio
async def test_hard_fail_stops_pipeline_before_save_but_advisory_fail_continues():
    hard_pipeline = _ValidationRunPipeline()
    hard_ctx = _validation_context(
        _report(HARD_FAIL, 0.31, [{"type": "ooc", "severity": 0.9}])
    )
    hard_result = await hard_pipeline.run_chapter(hard_ctx)
    assert hard_result.success is False
    assert hard_result.step_status["validate_content"] == "failed"
    assert hard_pipeline.save_attempted is False

    advisory_pipeline = _ValidationRunPipeline()
    advisory_ctx = _validation_context(
        _report(ADVISORY_FAIL, 0.58, [{"type": "style", "severity": 0.5}])
    )
    advisory_result = await advisory_pipeline.run_chapter(advisory_ctx)
    assert advisory_result.success is True
    assert advisory_result.step_status["validate_content"] == "warning"
    assert advisory_pipeline.save_attempted is True


@pytest.mark.asyncio
async def test_finalize_and_result_dict_preserve_validation_audit_snapshot():
    ctx = _validation_context(_report(ADVISORY_FAIL, 0.58, [{"severity": 0.5}]))
    await _Pipeline()._step_validate_content(ctx)
    await _Pipeline()._step_finalize(ctx)
    result = _Pipeline()._make_result(ctx, success=False, error="audit")
    payload = result.to_dict()

    assert ctx.audit_snapshot["validation_status"] == ADVISORY_FAIL
    assert payload["validation_status"] == ADVISORY_FAIL
    assert payload["validation_score"] == 0.58
    assert payload["audit_snapshot"]["validation_violations"] == [{"severity": 0.5}]

    unevaluated = PipelineResult(validation_score=None, validation_status=UNEVALUATED)
    assert unevaluated.to_dict()["validation_score"] is None


def test_themed_and_wuxia_audit_severity_refreshes_validation_status():
    for pipeline in (ThemedStoryPipeline("mystery"), WuxiaPipeline()):
        advisory_ctx = PipelineContext(
            validation_status=UNEVALUATED,
            validation_violations=[{"severity": 0.5}],
        )
        assert pipeline._refresh_validation_status(advisory_ctx) == ADVISORY_FAIL

        hard_ctx = PipelineContext(
            validation_status=UNEVALUATED,
            validation_violations=[{"severity": 0.75}],
        )
        assert pipeline._refresh_validation_status(hard_ctx) == HARD_FAIL
