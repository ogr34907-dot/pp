from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from domain.novel.entities.novel import AutopilotStatus, NovelStage
from engine.runtime.legacy_writing_delegate import run_legacy_writing


class _CountingLLM:
    def __init__(self) -> None:
        self.calls = 0

    async def generate(self, *_args, **_kwargs):
        self.calls += 1
        return SimpleNamespace(content="unused")


def _legacy_host_with_required_memory_failure(reason: str):
    llm_service = _CountingLLM()
    memory_engine = SimpleNamespace(
        bible_repository=object(),
        llm_service=llm_service,
    )
    context_builder = SimpleNamespace(
        budget_allocator=SimpleNamespace(memory_engine=memory_engine),
    )
    chapter_workflow = SimpleNamespace(
        context_builder=context_builder,
        prepare_chapter_generation=MagicMock(side_effect=RuntimeError(reason)),
        build_fallback_chapter_bundle=MagicMock(
            return_value={"context": "weak fallback context"}
        ),
    )
    host = SimpleNamespace(
        chapter_workflow=chapter_workflow,
        context_builder=context_builder,
        aftermath_pipeline=SimpleNamespace(_memory_engine=memory_engine),
        chapter_repository=SimpleNamespace(db=object()),
        story_node_repo=SimpleNamespace(
            get_by_novel=AsyncMock(
                return_value=[SimpleNamespace(node_type=SimpleNamespace(value="volume"))]
            )
        ),
        knowledge_service=None,
        _is_still_running=MagicMock(side_effect=[True, True, False]),
        _find_next_unwritten_chapter_async=AsyncMock(
            return_value=SimpleNamespace(
                number=2,
                outline="本章大纲",
                description="",
                title="第二章",
            )
        ),
        _sync_novel_current_act_from_chapter_story_node=MagicMock(),
        _cache_stats_to_shared_memory=MagicMock(),
        _update_shared_state=MagicMock(),
        _get_beat_sheet_for_chapter=AsyncMock(return_value=None),
        _flush_novel=MagicMock(),
    )
    return host, chapter_workflow, llm_service


@pytest.mark.asyncio
async def test_legacy_required_memory_failure_pauses_without_fallback_context():
    reason = "required_narrative_memory_unavailable:fact_lock_data_source"
    host, chapter_workflow, llm_service = _legacy_host_with_required_memory_failure(
        reason
    )
    novel = SimpleNamespace(
        novel_id=SimpleNamespace(value="novel-1"),
        current_stage=NovelStage.WRITING,
        autopilot_status=AutopilotStatus.RUNNING,
        target_chapters=20,
        max_auto_chapters=100,
        current_auto_chapters=1,
        last_chapter_tension=0,
        target_words_per_chapter=2500,
    )

    await run_legacy_writing(host, novel)

    chapter_workflow.build_fallback_chapter_bundle.assert_not_called()
    assert llm_service.calls == 0
    assert novel.current_stage == NovelStage.PAUSED_FOR_REVIEW
    assert novel.last_audit_narrative_ok is False
    host._update_shared_state.assert_any_call(
        "novel-1",
        current_stage=NovelStage.PAUSED_FOR_REVIEW.value,
        last_audit_narrative_ok=False,
        autopilot_pause_reason=reason,
    )


@pytest.mark.asyncio
async def test_legacy_evolution_gate_unavailable_pauses_without_fallback_or_llm():
    reason = "evolution_gate_unavailable:novel-1:chapter=2:backend down"
    host, chapter_workflow, llm_service = _legacy_host_with_required_memory_failure(
        reason
    )
    novel = SimpleNamespace(
        novel_id=SimpleNamespace(value="novel-1"),
        current_stage=NovelStage.WRITING,
        autopilot_status=AutopilotStatus.RUNNING,
        target_chapters=20,
        max_auto_chapters=100,
        current_auto_chapters=1,
        last_chapter_tension=0,
        target_words_per_chapter=2500,
    )

    await run_legacy_writing(host, novel)

    chapter_workflow.build_fallback_chapter_bundle.assert_not_called()
    assert llm_service.calls == 0
    assert novel.current_stage == NovelStage.PAUSED_FOR_REVIEW
    assert novel.autopilot_status == AutopilotStatus.STOPPED
    host._update_shared_state.assert_any_call(
        "novel-1",
        current_stage=NovelStage.PAUSED_FOR_REVIEW.value,
        autopilot_status=AutopilotStatus.STOPPED.value,
        paused_for_review=True,
        writing_substep="evolution_gate_unavailable",
        writing_substep_label="Evolution Gate unavailable; paused",
        autopilot_pause_reason=reason,
        evolution_gate_error=reason,
        evolution_gate_message="novel-1:chapter=2:backend down",
    )
    host._flush_novel.assert_called_once_with(novel)


@pytest.mark.asyncio
@pytest.mark.parametrize("target_chapters", [None, 0, True, 1.5, "invalid"])
async def test_legacy_writing_refuses_invalid_persisted_target_chapters(target_chapters):
    host = SimpleNamespace(
        _is_still_running=MagicMock(return_value=True),
        _update_shared_state=MagicMock(),
        _flush_novel=MagicMock(),
    )
    novel = SimpleNamespace(
        novel_id=SimpleNamespace(value="novel-without-target"),
        target_chapters=target_chapters,
        current_stage=NovelStage.WRITING,
        autopilot_status=AutopilotStatus.RUNNING,
    )

    await run_legacy_writing(host, novel)

    assert novel.current_stage == NovelStage.PAUSED_FOR_REVIEW
    assert novel.autopilot_status == AutopilotStatus.STOPPED
    host._update_shared_state.assert_called_once_with(
        "novel-without-target",
        current_stage=NovelStage.PAUSED_FOR_REVIEW.value,
        autopilot_pause_reason="target_chapters_required",
    )
    host._flush_novel.assert_called_once_with(novel)
