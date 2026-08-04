from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from domain.novel.entities.chapter import Chapter, ChapterStatus
from domain.novel.entities.novel import AutopilotStatus, NovelStage
from domain.novel.value_objects.novel_id import NovelId
from engine.runtime.audit_delegate import run_chapter_audit


@pytest.mark.asyncio
async def test_audit_pauses_before_aftermath_after_safe_snapshot_rewrite(monkeypatch):
    chapter = Chapter(
        id="chapter-1",
        novel_id=NovelId("novel-1"),
        number=1,
        title="第1章",
        content="旧正文",
        status=ChapterStatus.COMPLETED,
    )

    async def call_with_timeout(awaitable, **_kwargs):
        return await awaitable

    host = SimpleNamespace(
        _is_still_running=lambda _novel: True,
        _latest_completed_chapter_number=lambda _novel_id: 1,
        chapter_repository=SimpleNamespace(
            get_by_novel_and_number=lambda *_args: chapter
        ),
        _sync_novel_current_act_from_chapter_number=MagicMock(),
        _cache_stats_to_shared_memory=MagicMock(),
        _publish_audit_event=MagicMock(),
        _update_shared_state=MagicMock(),
        _call_with_timeout=call_with_timeout,
        _score_voice_only=AsyncMock(
            return_value={"drift_alert": True, "similarity_score": 0.3}
        ),
        _apply_voice_rewrite_loop=AsyncMock(
            return_value=(
                "新正文",
                {
                    "drift_alert": True,
                    "similarity_score": 0.3,
                    "rewrite_requires_rebuild": True,
                },
            )
        ),
        _flush_novel=MagicMock(),
    )
    novel = SimpleNamespace(
        novel_id=NovelId("novel-1"),
        current_stage=NovelStage.AUDITING,
        autopilot_status=AutopilotStatus.RUNNING,
        audit_progress=None,
    )
    monkeypatch.setattr(
        "engine.runtime.audit_delegate._write_autopilot_invocation_input",
        lambda **_kwargs: (_ for _ in ()).throw(
            AssertionError("safe snapshot must stop before later audit work")
        ),
    )

    await run_chapter_audit(host, novel)

    assert novel.current_stage == NovelStage.PAUSED_FOR_REVIEW
    assert novel.autopilot_status == AutopilotStatus.STOPPED
    host._flush_novel.assert_called_once_with(novel)


@pytest.mark.asyncio
async def test_audit_aftermath_uses_persisted_content_version(monkeypatch):
    chapter = Chapter(
        id="chapter-1",
        novel_id=NovelId("novel-1"),
        number=1,
        title="第1章",
        content="正文",
        status=ChapterStatus.COMPLETED,
        content_sha256="persisted-hash",
        content_revision=7,
    )
    running = iter((True, False))

    async def call_with_timeout(awaitable, **_kwargs):
        return await awaitable

    aftermath = SimpleNamespace(
        run_after_chapter_saved=AsyncMock(
            return_value={"narrative_sync_ok": True, "drift_alert": False}
        )
    )
    host = SimpleNamespace(
        _is_still_running=lambda _novel: next(running),
        _latest_completed_chapter_number=lambda _novel_id: 1,
        chapter_repository=SimpleNamespace(
            get_by_novel_and_number=lambda *_args: chapter
        ),
        _sync_novel_current_act_from_chapter_number=MagicMock(),
        _cache_stats_to_shared_memory=MagicMock(),
        _publish_audit_event=MagicMock(),
        _update_shared_state=MagicMock(),
        _call_with_timeout=call_with_timeout,
        _score_voice_only=AsyncMock(
            return_value={"drift_alert": False, "similarity_score": 0.9}
        ),
        _apply_voice_rewrite_loop=AsyncMock(
            return_value=(
                "正文",
                {"drift_alert": False, "similarity_score": 0.9},
            )
        ),
        _pending_story_pipeline_aftermath={},
        _pending_chapter_micro_beats={},
        aftermath_pipeline=aftermath,
    )
    novel = SimpleNamespace(
        novel_id=NovelId("novel-1"),
        current_stage=NovelStage.AUDITING,
        autopilot_status=AutopilotStatus.RUNNING,
        audit_progress=None,
    )
    monkeypatch.setattr(
        "engine.runtime.audit_delegate._write_autopilot_invocation_input",
        lambda **_kwargs: None,
    )
    monkeypatch.setattr(
        "engine.runtime.audit_delegate._consume_pending_payload",
        lambda *_args, **_kwargs: {},
    )
    monkeypatch.setattr(
        "engine.runtime.audit_delegate._read_shared_state",
        lambda _novel_id: {},
    )

    await run_chapter_audit(host, novel)

    assert aftermath.run_after_chapter_saved.await_args.kwargs == {
        "chapter_micro_beats": None,
        "voice_result": {"drift_alert": False, "similarity_score": 0.9},
        "expected_content_sha256": "persisted-hash",
        "expected_content_revision": 7,
    }
