"""StoryPipeline 写作委托测试"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from domain.novel.entities.novel import NovelStage
from engine.runtime.writing_delegate import (
    get_story_pipeline_mode,
    is_story_pipeline_writing_enabled,
    run_writing,
    run_story_pipeline_writing,
    story_pipeline_mode_was_unset,
)
import engine.runtime.writing_delegate as writing_delegate
from engine.pipeline.context import PipelineResult


def test_is_story_pipeline_writing_enabled_default_writing(monkeypatch):
    monkeypatch.delenv("PLOTPILOT_USE_STORY_PIPELINE", raising=False)
    assert is_story_pipeline_writing_enabled() is True


def test_is_story_pipeline_writing_enabled_explicit_off(monkeypatch):
    monkeypatch.setenv("PLOTPILOT_USE_STORY_PIPELINE", "off")
    assert is_story_pipeline_writing_enabled() is False


def test_get_story_pipeline_mode_default_writing(monkeypatch):
    monkeypatch.delenv("PLOTPILOT_USE_STORY_PIPELINE", raising=False)
    assert get_story_pipeline_mode() == "writing"
    assert story_pipeline_mode_was_unset() is True


def test_is_story_pipeline_writing_enabled_on(monkeypatch):
    monkeypatch.setenv("PLOTPILOT_USE_STORY_PIPELINE", "1")
    assert is_story_pipeline_writing_enabled() is True


def test_get_story_pipeline_mode_full(monkeypatch):
    monkeypatch.setenv("PLOTPILOT_USE_STORY_PIPELINE", "full")
    assert get_story_pipeline_mode() == "full"
    assert story_pipeline_mode_was_unset() is False
    assert is_story_pipeline_writing_enabled() is True


def test_get_story_pipeline_mode_unknown_warns_and_defaults(monkeypatch, caplog):
    monkeypatch.setenv("PLOTPILOT_USE_STORY_PIPELINE", "surprise")

    assert get_story_pipeline_mode() == "writing"
    assert "未知 PLOTPILOT_USE_STORY_PIPELINE" in caplog.text


@pytest.mark.asyncio
async def test_run_story_pipeline_writing_success_updates_novel(monkeypatch):
    novel = MagicMock()
    novel.novel_id.value = "novel-1"
    novel.genre = "wuxia"
    novel.target_words_per_chapter = 3000
    novel.auto_approve_mode = True
    novel.era = "ancient"
    novel.current_auto_chapters = 2
    novel.current_chapter_in_act = 1
    novel.current_beat_index = 3
    novel.beats_completed = True

    daemon = MagicMock()
    daemon._update_shared_state = MagicMock()
    daemon._flush_novel = MagicMock()

    mock_ctx = MagicMock()
    mock_ctx.chapter_number = 5
    mock_runner = MagicMock()
    mock_runner.DEFAULT_TARGET_WORDS = 2500
    mock_runner._make_context.return_value = mock_ctx
    mock_runner._get_novel_phase.return_value = "development"

    mock_pipeline = MagicMock()
    mock_pipeline.run_chapter = AsyncMock(
        return_value=PipelineResult(
            success=True,
            chapter_number=5,
            word_count=2800,
            tension=72,
        )
    )
    commit_repository = MagicMock()
    commit_repository.recover_pending_story_pipeline_advances.return_value = []
    commit_repository.advance_story_pipeline_once.return_value = SimpleNamespace(
        disposition="applied",
        current_auto_chapters=3,
        current_chapter_in_act=2,
        current_stage="auditing",
        failure_reason="",
    )
    monkeypatch.setattr(
        writing_delegate,
        "_get_story_pipeline_commit_repository",
        lambda _runner: commit_repository,
        raising=False,
    )

    with patch("engine.runtime.writing_delegate._build_runner", return_value=mock_runner), patch(
        "engine.pipelines.registry.get_pipeline_registry"
    ) as mock_registry:
        mock_registry.return_value.create_pipeline.return_value = mock_pipeline
        await run_story_pipeline_writing(daemon, novel)

    assert novel.current_auto_chapters == 3
    assert novel.current_chapter_in_act == 2
    assert novel.current_beat_index == 0
    assert novel.beats_completed is False
    assert novel.last_chapter_tension == 72
    assert novel.current_stage == NovelStage.AUDITING
    assert commit_repository.recover_pending_story_pipeline_advances.call_args.kwargs[
        "require_memory_sync"
    ] is True
    assert commit_repository.advance_story_pipeline_once.call_args.kwargs[
        "require_memory_sync"
    ] is True
    daemon._flush_novel.assert_called_once_with(novel)


@pytest.mark.asyncio
async def test_run_story_pipeline_writing_all_chapters_done_transitions_act():
    novel = MagicMock()
    novel.novel_id.value = "novel-1"
    novel.genre = ""
    novel.current_act = 1
    novel.current_chapter_in_act = 5

    daemon = MagicMock()
    daemon._update_shared_state = MagicMock()
    daemon._flush_novel = MagicMock()
    daemon._current_act_fully_written = AsyncMock(return_value=True)

    mock_runner = MagicMock()
    mock_runner.DEFAULT_TARGET_WORDS = 2500
    mock_runner._make_context.return_value = MagicMock(chapter_number=0)
    mock_runner._get_novel_phase.return_value = "development"

    mock_pipeline = MagicMock()
    mock_pipeline.run_chapter = AsyncMock(
        return_value=PipelineResult(success=False, error="所有章节已写完，无需继续")
    )

    with patch("engine.runtime.writing_delegate._build_runner", return_value=mock_runner), patch(
        "engine.pipelines.registry.get_pipeline_registry"
    ) as mock_registry:
        mock_registry.return_value.create_pipeline.return_value = mock_pipeline
        await run_story_pipeline_writing(daemon, novel)

    assert novel.current_act == 2
    assert novel.current_chapter_in_act == 0
    assert novel.current_stage == NovelStage.ACT_PLANNING
    daemon._flush_novel.assert_called_once_with(novel)


@pytest.mark.asyncio
async def test_run_story_pipeline_writing_all_chapters_done_without_full_act_replans_same_act():
    novel = MagicMock()
    novel.novel_id.value = "novel-1"
    novel.genre = ""
    novel.current_act = 0
    novel.current_chapter_in_act = 3

    daemon = MagicMock()
    daemon._update_shared_state = MagicMock()
    daemon._flush_novel = MagicMock()
    daemon._current_act_fully_written = AsyncMock(return_value=False)

    mock_runner = MagicMock()
    mock_runner.DEFAULT_TARGET_WORDS = 2500
    mock_runner._make_context.return_value = MagicMock(chapter_number=0)
    mock_runner._get_novel_phase.return_value = "opening"

    mock_pipeline = MagicMock()
    mock_pipeline.run_chapter = AsyncMock(
        return_value=PipelineResult(success=False, error="所有章节已写完，无需继续")
    )

    with patch("engine.runtime.writing_delegate._build_runner", return_value=mock_runner), patch(
        "engine.pipelines.registry.get_pipeline_registry"
    ) as mock_registry:
        mock_registry.return_value.create_pipeline.return_value = mock_pipeline
        await run_story_pipeline_writing(daemon, novel)

    assert novel.current_act == 0
    assert novel.current_chapter_in_act == 0
    assert novel.current_stage == NovelStage.ACT_PLANNING
    daemon._flush_novel.assert_called_once_with(novel)


@pytest.mark.asyncio
async def test_run_story_pipeline_writing_pauses_on_canonical_history_failure():
    novel = MagicMock()
    novel.novel_id.value = "novel-history"
    novel.genre = ""
    novel.target_words_per_chapter = 2500
    novel.auto_approve_mode = True
    novel.era = "ancient"

    daemon = MagicMock()
    daemon._update_shared_state = MagicMock()
    daemon._flush_novel = MagicMock()

    mock_runner = MagicMock()
    mock_runner.DEFAULT_TARGET_WORDS = 2500
    mock_runner._make_context.return_value = MagicMock(chapter_number=2)
    mock_runner._get_novel_phase.return_value = "development"
    mock_pipeline = MagicMock()
    mock_pipeline.run_chapter = AsyncMock(
        return_value=PipelineResult(
            success=False,
            error="canonical_history_checkpoint_required",
        )
    )

    with patch("engine.runtime.writing_delegate._build_runner", return_value=mock_runner), patch(
        "engine.pipelines.registry.get_pipeline_registry"
    ) as mock_registry:
        mock_registry.return_value.create_pipeline.return_value = mock_pipeline
        await run_story_pipeline_writing(daemon, novel)

    assert novel.current_stage == NovelStage.PAUSED_FOR_REVIEW
    assert novel.last_audit_narrative_ok is False
    daemon._update_shared_state.assert_any_call(
        "novel-history",
        current_stage="paused_for_review",
        last_audit_narrative_ok=False,
        autopilot_pause_reason="canonical_history_checkpoint_required",
    )
    daemon._flush_novel.assert_called_once_with(novel)


@pytest.mark.asyncio
async def test_run_story_pipeline_writing_recovers_pending_advance_without_regenerating_prose(
    monkeypatch,
):
    novel = MagicMock()
    novel.novel_id.value = "novel-recovery"
    novel.genre = ""
    novel.target_words_per_chapter = 2500
    novel.auto_approve_mode = True
    novel.era = "ancient"
    novel.current_auto_chapters = 2
    novel.current_chapter_in_act = 1
    novel.current_beat_index = 3
    novel.beats_completed = True

    daemon = MagicMock()
    daemon._update_shared_state = MagicMock()
    daemon._flush_novel = MagicMock()

    mock_runner = MagicMock()
    mock_runner.DEFAULT_TARGET_WORDS = 2500
    commit_repository = MagicMock()
    commit_repository.recover_pending_story_pipeline_advances.return_value = [
        SimpleNamespace(
            disposition="applied",
            chapter_number=3,
            current_auto_chapters=3,
            current_chapter_in_act=2,
            current_stage="auditing",
            failure_reason="",
        )
    ]
    monkeypatch.setattr(
        writing_delegate,
        "_get_story_pipeline_commit_repository",
        lambda _runner: commit_repository,
        raising=False,
    )
    mock_pipeline = MagicMock()
    mock_pipeline.run_chapter = AsyncMock(
        side_effect=AssertionError("canonical prose must not be regenerated")
    )

    with patch("engine.runtime.writing_delegate._build_runner", return_value=mock_runner), patch(
        "engine.pipelines.registry.get_pipeline_registry"
    ) as mock_registry:
        mock_registry.return_value.create_pipeline.return_value = mock_pipeline
        await run_story_pipeline_writing(daemon, novel)

    assert novel.current_auto_chapters == 3
    assert novel.current_chapter_in_act == 2
    assert novel.current_beat_index == 0
    assert novel.beats_completed is False
    assert novel.current_stage == NovelStage.AUDITING
    mock_registry.assert_not_called()
    daemon._flush_novel.assert_called_once_with(novel)


@pytest.mark.asyncio
async def test_run_story_pipeline_writing_pauses_when_canonical_advance_is_not_current(
    monkeypatch,
):
    novel = MagicMock()
    novel.novel_id.value = "novel-stale-advance"
    novel.genre = ""
    novel.target_words_per_chapter = 2500
    novel.auto_approve_mode = True
    novel.era = "ancient"
    novel.current_auto_chapters = 2
    novel.current_chapter_in_act = 1

    daemon = MagicMock()
    daemon._update_shared_state = MagicMock()
    daemon._flush_novel = MagicMock()

    mock_runner = MagicMock()
    mock_runner.DEFAULT_TARGET_WORDS = 2500
    mock_runner._make_context.return_value = MagicMock(chapter_number=3)
    mock_runner._get_novel_phase.return_value = "development"
    commit_repository = MagicMock()
    commit_repository.recover_pending_story_pipeline_advances.return_value = []
    commit_repository.advance_story_pipeline_once.return_value = SimpleNamespace(
        disposition="source_version_mismatch",
        failure_reason="canonical_commit_not_current",
    )
    monkeypatch.setattr(
        writing_delegate,
        "_get_story_pipeline_commit_repository",
        lambda _runner: commit_repository,
        raising=False,
    )
    mock_pipeline = MagicMock()
    mock_pipeline.run_chapter = AsyncMock(
        return_value=PipelineResult(success=True, chapter_number=3, word_count=2800)
    )

    with patch("engine.runtime.writing_delegate._build_runner", return_value=mock_runner), patch(
        "engine.pipelines.registry.get_pipeline_registry"
    ) as mock_registry:
        mock_registry.return_value.create_pipeline.return_value = mock_pipeline
        await run_story_pipeline_writing(daemon, novel)

    assert novel.current_stage == NovelStage.PAUSED_FOR_REVIEW
    assert novel.current_auto_chapters == 2
    assert novel.current_chapter_in_act == 1
    daemon._flush_novel.assert_called_once_with(novel)


@pytest.mark.asyncio
async def test_run_story_pipeline_writing_pauses_when_required_narrative_memory_is_unavailable():
    novel = MagicMock()
    novel.novel_id.value = "novel-memory"
    novel.genre = ""
    novel.target_words_per_chapter = 2500
    novel.auto_approve_mode = True
    novel.era = "ancient"

    daemon = MagicMock()
    daemon._update_shared_state = MagicMock()
    daemon._flush_novel = MagicMock()

    mock_runner = MagicMock()
    mock_runner.DEFAULT_TARGET_WORDS = 2500
    mock_runner._make_context.return_value = MagicMock(chapter_number=2)
    mock_runner._get_novel_phase.return_value = "development"
    mock_pipeline = MagicMock()
    mock_pipeline.run_chapter = AsyncMock(
        return_value=PipelineResult(
            success=False,
            error="required_narrative_memory_unavailable:memory_engine",
        )
    )

    with patch("engine.runtime.writing_delegate._build_runner", return_value=mock_runner), patch(
        "engine.pipelines.registry.get_pipeline_registry"
    ) as mock_registry:
        mock_registry.return_value.create_pipeline.return_value = mock_pipeline
        await run_story_pipeline_writing(daemon, novel)

    assert novel.current_stage == NovelStage.PAUSED_FOR_REVIEW
    assert novel.last_audit_narrative_ok is False
    daemon._update_shared_state.assert_any_call(
        "novel-memory",
        current_stage="paused_for_review",
        last_audit_narrative_ok=False,
        autopilot_pause_reason="required_narrative_memory_unavailable:memory_engine",
    )
    daemon._flush_novel.assert_called_once_with(novel)


@pytest.mark.asyncio
async def test_run_story_pipeline_writing_pauses_when_required_auxiliary_state_is_unavailable():
    novel = MagicMock()
    novel.novel_id.value = "novel-auxiliary"
    novel.genre = ""
    novel.target_words_per_chapter = 2500
    novel.auto_approve_mode = True
    novel.era = "ancient"

    daemon = MagicMock()
    daemon._update_shared_state = MagicMock()
    daemon._flush_novel = MagicMock()

    mock_runner = MagicMock()
    mock_runner.DEFAULT_TARGET_WORDS = 2500
    mock_runner._make_context.return_value = MagicMock(chapter_number=2)
    mock_runner._get_novel_phase.return_value = "development"
    mock_pipeline = MagicMock()
    mock_pipeline.run_chapter = AsyncMock(
        return_value=PipelineResult(
            success=False,
            error="required_auxiliary_state_sync_failed:evolution snapshot persistence failed",
        )
    )

    with patch("engine.runtime.writing_delegate._build_runner", return_value=mock_runner), patch(
        "engine.pipelines.registry.get_pipeline_registry"
    ) as mock_registry:
        mock_registry.return_value.create_pipeline.return_value = mock_pipeline
        await run_story_pipeline_writing(daemon, novel)

    assert novel.current_stage == NovelStage.PAUSED_FOR_REVIEW
    assert novel.last_audit_narrative_ok is False
    daemon._update_shared_state.assert_any_call(
        "novel-auxiliary",
        current_stage="paused_for_review",
        last_audit_narrative_ok=False,
        autopilot_pause_reason=(
            "required_auxiliary_state_sync_failed:evolution snapshot persistence failed"
        ),
    )
    daemon._flush_novel.assert_called_once_with(novel)


@pytest.mark.asyncio
async def test_run_writing_blocks_legacy_direct_writers_before_candidate_run_exists():
    host = SimpleNamespace(
        use_story_pipeline_for_writing=False,
        chapter_repository=SimpleNamespace(
            db=SimpleNamespace(fetch_one=MagicMock(return_value=None))
        ),
        _update_shared_state=MagicMock(),
        _flush_novel=MagicMock(),
    )
    novel = SimpleNamespace(novel_id=SimpleNamespace(value="pre-run-novel"))

    with patch("engine.runtime.legacy_writing_delegate.run_legacy_writing", new=AsyncMock()) as legacy:
        await run_writing(host, novel)

    legacy.assert_not_awaited()
    host._flush_novel.assert_called_once_with(novel)


@pytest.mark.asyncio
@pytest.mark.parametrize("pipeline_enabled", [False, True])
async def test_run_writing_blocks_legacy_direct_writers_for_candidate_first_novel(
    pipeline_enabled,
):
    db = SimpleNamespace(fetch_one=MagicMock(return_value={"state": "running"}))
    host = SimpleNamespace(
        use_story_pipeline_for_writing=pipeline_enabled,
        chapter_repository=SimpleNamespace(db=db),
        _update_shared_state=MagicMock(),
        _flush_novel=MagicMock(),
    )
    novel = SimpleNamespace(novel_id=SimpleNamespace(value="candidate-novel"))

    with patch(
        "engine.runtime.writing_delegate.run_story_pipeline_writing",
        new_callable=AsyncMock,
    ) as pipeline, patch(
        "engine.runtime.legacy_writing_delegate.run_legacy_writing",
        new_callable=AsyncMock,
    ) as legacy:
        await run_writing(host, novel)

    pipeline.assert_not_awaited()
    legacy.assert_not_awaited()
    host._update_shared_state.assert_called_once_with(
        "candidate-novel",
        current_stage="paused_for_review",
        writing_substep="candidate_first_required",
        writing_substep_label="候选稿流程正在管理正式章节",
        autopilot_pause_reason="candidate_first_required",
    )
    host._flush_novel.assert_called_once_with(novel)
