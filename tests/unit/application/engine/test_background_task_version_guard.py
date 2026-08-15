from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from application.engine.services.background_task_service import (
    BackgroundTask,
    BackgroundTaskService,
    TaskType,
)
from application.core.services.chapter_rewrite_coordinator import ChapterRewriteResult
from domain.novel.entities.chapter import Chapter, ChapterStatus
from domain.novel.value_objects.chapter_id import ChapterId
from domain.novel.value_objects.novel_id import NovelId
from engine.runtime.daemon_host import DaemonHostMixin


@pytest.mark.asyncio
async def test_background_bundle_task_passes_captured_content_version(monkeypatch):
    service = BackgroundTaskService.__new__(BackgroundTaskService)
    service.llm_service = object()
    service.knowledge_service = object()
    service.chapter_indexing_service = None
    service.triple_repository = None
    service.foreshadowing_repo = None
    service.storyline_repository = None
    service.chapter_repository = object()
    service.plot_arc_repository = None
    service.narrative_event_repository = None
    captured = AsyncMock()
    monkeypatch.setattr(
        "application.world.services.chapter_narrative_sync.sync_chapter_narrative_after_save",
        captured,
    )
    task = BackgroundTask(
        task_id="task-1",
        task_type=TaskType.EXTRACT_BUNDLE,
        novel_id=NovelId("novel-1"),
        chapter_id=ChapterId("chapter-1"),
        payload={
            "content": "正文",
            "chapter_number": 1,
            "content_sha256": "hash-1",
            "content_revision": 3,
        },
    )

    await service._handle_extract_bundle_async(task)

    assert captured.await_args.kwargs["expected_content_sha256"] == "hash-1"
    assert captured.await_args.kwargs["expected_content_revision"] == 3


def test_legacy_daemon_bundle_submission_captures_current_content_version():
    submitted = []
    chapter = SimpleNamespace(
        content="正文",
        content_sha256="hash-current",
        content_revision=6,
    )
    host = SimpleNamespace(
        background_task_service=SimpleNamespace(
            submit_task=lambda **kwargs: submitted.append(kwargs)
        ),
        chapter_repository=SimpleNamespace(
            get_by_novel_and_number=lambda *_args: chapter
        ),
        voice_drift_service=None,
    )
    novel = SimpleNamespace(novel_id=NovelId("novel-1"))

    DaemonHostMixin._legacy_auditing_tasks_and_voice(
        host,
        novel,
        1,
        "正文",
        ChapterId("chapter-1"),
    )

    bundle = next(item for item in submitted if item["task_type"] == TaskType.EXTRACT_BUNDLE)
    assert bundle["payload"]["content_sha256"] == "hash-current"
    assert bundle["payload"]["content_revision"] == 6


def test_daemon_ephemeral_content_save_updates_version_columns():
    queued = []
    host = SimpleNamespace(_queue_sql=lambda sql, params: queued.append((sql, params)) or True)

    assert DaemonHostMixin._save_chapter_ephemeral(
        host,
        "novel-1",
        1,
        content="新正文",
    )

    sql, params = queued[0]
    assert "content_sha256 = ?" in sql
    assert "content_revision = CASE" in sql
    assert "新正文" in params


@pytest.mark.asyncio
async def test_daemon_completed_overwrite_pauses_for_candidate_first_authority():
    updates = []
    flushed = []
    host = SimpleNamespace(
        chapter_repository=SimpleNamespace(db=SimpleNamespace()),
        _push_persistence_command=lambda *_args, **_kwargs: True,
        _queue_sql=lambda *_args, **_kwargs: True,
        _save_chapter_ephemeral=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("completed overwrite must not bypass candidate authority")
        ),
        _update_shared_state=lambda *args, **kwargs: updates.append((args, kwargs)),
        _flush_novel=lambda novel: flushed.append(novel),
    )
    novel = SimpleNamespace(novel_id=NovelId("novel-1"), generation_prefs=None)
    chapter_node = SimpleNamespace(number=1, id="chapter-1", title="第1章", outline="")

    result = await DaemonHostMixin._upsert_chapter_content(
        host,
        novel,
        chapter_node,
        "新正文",
        status="completed",
    )

    assert result is False
    assert novel.autopilot_status.value == "stopped"
    assert novel.current_stage.value == "paused_for_review"
    assert updates == [
        (
            ("novel-1",),
            {
                "current_stage": "paused_for_review",
                "writing_substep": "candidate_first_required",
                "writing_substep_label": "候选稿流程正在管理正式章节",
                "autopilot_pause_reason": "candidate_first_required",
            },
        )
    ]
    assert flushed == [novel]


@pytest.mark.asyncio
async def test_voice_rewrite_uses_rewrite_coordinator_and_requires_rebuild(monkeypatch):
    chapter = Chapter(
        id="chapter-1",
        novel_id=NovelId("novel-1"),
        number=1,
        title="第1章",
        content="旧正文",
        status=ChapterStatus.COMPLETED,
    )
    calls = []

    class _Coordinator:
        def rewrite(self, existing, content, *, rewrite_mode):
            calls.append((existing, content, rewrite_mode))
            return ChapterRewriteResult(
                chapter=existing,
                rewrite_mode=rewrite_mode,
                requires_rebuild=True,
                replay_completed=False,
            )

    monkeypatch.setattr(
        "application.core.services.chapter_rewrite_coordinator.ChapterRewriteCoordinator.for_chapter_repository",
        lambda *_args, **_kwargs: _Coordinator(),
    )
    host = SimpleNamespace(
        chapter_repository=SimpleNamespace(),
        _should_attempt_voice_rewrite=lambda result: bool(result.get("drift_alert")),
        _is_still_running=lambda _novel: True,
        _rewrite_chapter_for_voice=AsyncMock(return_value="新正文"),
        _save_chapter_ephemeral=lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("voice rewrite must not bypass rewrite coordinator")
        ),
        _score_voice_only=AsyncMock(),
    )
    novel = SimpleNamespace(novel_id=NovelId("novel-1"))

    content, result = await DaemonHostMixin._apply_voice_rewrite_loop(
        host,
        novel,
        chapter,
        "旧正文",
        {"drift_alert": True, "similarity_score": 0.3},
    )

    assert content == "新正文"
    assert result["rewrite_requires_rebuild"] is True
    assert calls == [(chapter, "新正文", "safe_snapshot")]
    host._score_voice_only.assert_not_awaited()
