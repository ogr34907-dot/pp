import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from domain.novel.entities.novel import NovelStage
from engine.pipeline.context import PipelineResult
from engine.runtime import writing_delegate
from infrastructure.persistence.database.connection import DatabaseConnection


def _novel(novel_id: str = "novel-1") -> SimpleNamespace:
    return SimpleNamespace(
        novel_id=SimpleNamespace(value=novel_id),
        genre="",
        generation_prefs=None,
        target_words_per_chapter=2500,
        auto_approve_mode=True,
        era="ancient",
        current_auto_chapters=62,
        current_chapter_in_act=4,
        current_beat_index=0,
        beats_completed=False,
        current_stage=NovelStage.WRITING,
        last_audit_narrative_ok=True,
        last_chapter_tension=0,
    )


def _runner(database=None, *, chapter_number: int = 64) -> MagicMock:
    runner = MagicMock()
    runner.DEFAULT_TARGET_WORDS = 2500
    runner._get_novel_phase.return_value = "development"
    runner._make_context.return_value = MagicMock(chapter_number=chapter_number)
    runner.chapter_repository.db = database
    return runner


def _seed_terminal_failure(database: DatabaseConnection) -> None:
    content = "chapter 63 content"
    content_sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()
    database.execute(
        "INSERT INTO novels (id, title, slug) VALUES ('novel-1', 'Demo', 'demo')"
    )
    database.execute(
        "INSERT INTO chapters "
        "(id, novel_id, number, content, content_sha256, content_revision, status) "
        "VALUES ('chapter-63', 'novel-1', 63, ?, ?, 2, 'completed')",
        (content, content_sha256),
    )
    database.execute(
        "INSERT INTO chapter_narrative_commits "
        "(novel_id, chapter_number, content_sha256, pipeline_version, content_revision, "
        "status, failure_reason, attempt_count) "
        "VALUES ('novel-1', 63, ?, 'chapter-narrative-sync:v1', 2, "
        "'failed', 'API returned empty content', 3)",
        (content_sha256,),
    )
    database.commit()


@pytest.mark.asyncio
async def test_canonical_pause_publishes_durable_chapter_reason_and_counts(tmp_path):
    database = DatabaseConnection(str(tmp_path / "canonical-pause.db"))
    _seed_terminal_failure(database)
    runner = _runner(database)
    daemon = MagicMock()
    daemon._read_chapter_stats_ephemeral.return_value = (63, 63, 250000)
    pipeline = MagicMock()
    pipeline.run_chapter = AsyncMock(
        return_value=PipelineResult(
            success=False,
            chapter_number=64,
            error="canonical_aftermath_not_ready",
        )
    )

    with patch("engine.runtime.writing_delegate._build_runner", return_value=runner), patch(
        "engine.pipelines.registry.get_pipeline_registry"
    ) as registry:
        registry.return_value.create_pipeline.return_value = pipeline
        await writing_delegate.run_story_pipeline_writing(daemon, _novel())

    published = daemon._update_shared_state.call_args.kwargs
    assert published["autopilot_pause_reason"] == "canonical_aftermath_not_ready"
    assert published["canonical_aftermath_chapter_number"] == 63
    assert published["canonical_aftermath_failure_reason"] == "API returned empty content"
    assert published["current_chapter_number"] == 63
    assert published["_cached_completed_chapters"] == 63
    assert published["_cached_manuscript_chapters"] == 63
    assert published["_cached_total_words"] == 250000


@pytest.mark.asyncio
async def test_successful_pending_advance_clears_stale_canonical_gate_and_publishes_counts(
    monkeypatch,
):
    novel = _novel("novel-recovery")
    daemon = MagicMock()
    daemon._read_chapter_stats_ephemeral.return_value = (63, 63, 250000)
    runner = _runner()
    commit_repository = MagicMock()
    commit_repository.recover_pending_story_pipeline_advances.return_value = [
        SimpleNamespace(
            disposition="applied",
            chapter_number=63,
            current_auto_chapters=63,
            current_chapter_in_act=5,
            current_stage="auditing",
            failure_reason="",
        )
    ]
    monkeypatch.setattr(
        writing_delegate,
        "_get_story_pipeline_commit_repository",
        lambda _runner: commit_repository,
    )

    with patch("engine.runtime.writing_delegate._build_runner", return_value=runner), patch(
        "engine.pipelines.registry.get_pipeline_registry"
    ) as registry:
        await writing_delegate.run_story_pipeline_writing(daemon, novel)

    registry.assert_not_called()
    published = daemon._update_shared_state.call_args.kwargs
    assert published["autopilot_pause_reason"] == ""
    assert published["canonical_aftermath_chapter_number"] is None
    assert published["canonical_aftermath_failure_reason"] == ""
    assert published["requires_ai_review"] is False
    assert published["_cached_completed_chapters"] == 63
    assert published["_cached_manuscript_chapters"] == 63
    assert published["_cached_total_words"] == 250000
    assert published["_cached_current_chapter_number"] == 63
