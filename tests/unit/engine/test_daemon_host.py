"""DaemonHostMixin Phase 7/8/9 测试"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from application.engine.services.autopilot_daemon import AutopilotDaemon
from domain.novel.entities.chapter import ChapterStatus
from engine.runtime.daemon_host import DaemonHostMixin
from engine.runtime.runner import StoryPipelineRunner


def test_autopilot_daemon_inherits_daemon_host_mixin():
    assert issubclass(AutopilotDaemon, DaemonHostMixin)


def test_story_pipeline_runner_inherits_daemon_host_mixin():
    assert issubclass(StoryPipelineRunner, DaemonHostMixin)


def test_daemon_host_mixin_provides_infrastructure_methods():
    assert hasattr(DaemonHostMixin, "_update_shared_state")
    assert hasattr(DaemonHostMixin, "_call_with_timeout")
    assert hasattr(DaemonHostMixin, "_find_next_unwritten_chapter_async")
    assert hasattr(DaemonHostMixin, "_flush_novel")


def test_autopilot_daemon_keeps_entrypoint_methods():
    assert hasattr(AutopilotDaemon, "run_forever")
    assert hasattr(AutopilotDaemon, "_process_novel")
    assert hasattr(AutopilotDaemon, "_handle_writing")


def test_autopilot_daemon_emits_deprecation_warning():
    with pytest.warns(DeprecationWarning, match="AutopilotDaemon"):
        AutopilotDaemon(
            novel_repository=object(),
            llm_service=object(),
            context_builder=None,
            background_task_service=None,
            planning_service=None,
            story_node_repo=None,
            chapter_repository=None,
        )


def test_story_pipeline_runner_is_self_hosted():
    runner = StoryPipelineRunner(
        novel_repository=object(),
        llm_service=object(),
        context_builder=None,
        background_task_service=None,
        planning_service=None,
        story_node_repo=None,
        chapter_repository=None,
        use_story_pipeline_for_writing=True,
    )
    assert runner.host is runner
    assert runner.use_story_pipeline_for_writing is True


@pytest.mark.asyncio
async def test_audit_number_cannot_force_draft_chapter_completed_without_canonical_claim():
    host = DaemonHostMixin.__new__(DaemonHostMixin)
    node = SimpleNamespace(
        number=1,
        node_type=SimpleNamespace(value="chapter"),
    )
    chapter = SimpleNamespace(status=ChapterStatus.DRAFT)
    host.story_node_repo = SimpleNamespace(get_by_novel=AsyncMock(return_value=[node]))
    host.chapter_repository = SimpleNamespace(
        get_by_novel_and_number=MagicMock(return_value=chapter)
    )
    host._save_chapter_ephemeral = MagicMock()
    host._is_chapter_narrative_ready = MagicMock(return_value=False)
    novel = SimpleNamespace(
        novel_id=SimpleNamespace(value="novel-1"),
        last_audit_chapter_number=1,
    )

    result = await host._find_next_unwritten_chapter_async(novel)

    assert result is node
    assert chapter.status == ChapterStatus.DRAFT
    host._save_chapter_ephemeral.assert_not_called()

