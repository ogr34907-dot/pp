"""Phase 5/6 runtime delegates 测试"""
from unittest.mock import AsyncMock, MagicMock, patch
from types import SimpleNamespace

import pytest

from domain.novel.entities.novel import AutopilotStatus, NovelStage
from engine.runtime.act_planning_delegate import run_act_planning
from engine.runtime.audit_delegate import _audit_pause_gate
from engine.runtime.legacy_writing_delegate import _legacy_chapter_is_continuation_ready
from engine.runtime.macro_planning_delegate import run_macro_planning
from engine.runtime.novel_lifecycle import process_novel
from engine.runtime.writing_delegate import run_writing


def test_audit_pause_gate_always_blocks_canonical_failure():
    prefs = SimpleNamespace(
        pause_after_each_chapter_audit=False,
        audit_pause_on_hard_fail=False,
        audit_pause_on_anti_ai_severe=False,
    )

    assert _audit_pause_gate(
        canonical_ready=False,
        auto=True,
        prefs=prefs,
        hard_fail=False,
        anti_ai_severe=False,
    ) is True


@pytest.mark.parametrize("hard_fail,anti_ai_severe", [(True, False), (False, True)])
def test_audit_pause_gate_blocks_severe_failures_in_full_auto_mode(
    hard_fail, anti_ai_severe
):
    prefs = SimpleNamespace(
        pause_after_each_chapter_audit=False,
        audit_pause_on_hard_fail=False,
        audit_pause_on_anti_ai_severe=False,
    )

    assert _audit_pause_gate(
        canonical_ready=True,
        auto=True,
        prefs=prefs,
        hard_fail=hard_fail,
        anti_ai_severe=anti_ai_severe,
    ) is True


def test_audit_pause_gate_full_auto_does_not_pause_on_advisory_result():
    prefs = SimpleNamespace(
        pause_after_each_chapter_audit=True,
        audit_pause_on_hard_fail=True,
        audit_pause_on_anti_ai_severe=True,
    )

    assert _audit_pause_gate(
        canonical_ready=True,
        auto=True,
        prefs=prefs,
        hard_fail=False,
        anti_ai_severe=False,
    ) is False


def test_legacy_chapter_cannot_skip_from_audit_number_without_canonical_claim():
    host = MagicMock()
    host._is_chapter_narrative_ready.return_value = False
    chapter = SimpleNamespace(status=SimpleNamespace(value="completed"))

    assert _legacy_chapter_is_continuation_ready(
        host,
        "novel-1",
        1,
        chapter,
    ) is False


@pytest.mark.asyncio
async def test_legacy_gate_receives_next_node_chapter_and_outline_before_writing():
    from engine.runtime.legacy_writing_delegate import run_legacy_writing

    class _StopAfterGate(RuntimeError):
        pass

    gate_calls = []
    next_node = SimpleNamespace(
        number=7,
        outline="第七章正式章纲",
        description="第七章描述",
        title="第七章",
    )
    volume_node = SimpleNamespace(node_type=SimpleNamespace(value="volume"))
    host = MagicMock()
    host._is_still_running.return_value = True
    host.story_node_repo.get_by_novel = AsyncMock(return_value=[volume_node])
    host._find_next_unwritten_chapter_async = AsyncMock(return_value=next_node)
    host.hierarchy_gate = object()

    async def capture_gate(*args, **kwargs):
        gate_calls.append((args, kwargs))
        raise _StopAfterGate()

    with (
        patch(
            "engine.runtime.legacy_writing_delegate.enforce_chapter_candidate",
            new=capture_gate,
        ),
        patch(
            "engine.runtime.legacy_writing_delegate.candidate_from_outline",
            side_effect=lambda outline, node: {"outline": outline, "chapter": node.number},
        ),
    ):
        novel = SimpleNamespace(
            novel_id=SimpleNamespace(value="novel-1"),
            target_chapters=10,
            max_auto_chapters=99,
            current_auto_chapters=0,
            last_chapter_tension=0,
        )

        with pytest.raises(_StopAfterGate):
            await run_legacy_writing(host, novel)

    assert len(gate_calls) == 1
    _, kwargs = gate_calls[0]
    assert kwargs["chapter_number"] == 7
    assert kwargs["chapter_node"] is next_node
    assert kwargs["candidate"]["outline"] == "第七章正式章纲"


@pytest.mark.asyncio
async def test_process_novel_routes_macro_planning():
    from domain.novel.entities.novel import AutopilotStatus, NovelStage

    host = MagicMock()
    host._is_still_running.return_value = True
    host.circuit_breaker = None
    novel = MagicMock()
    novel.novel_id.value = "n-1"
    novel.current_stage = NovelStage.MACRO_PLANNING
    novel.autopilot_status = AutopilotStatus.RUNNING

    with patch(
        "engine.runtime.novel_lifecycle.run_macro_planning",
        new_callable=AsyncMock,
    ) as mock_macro:
        await process_novel(host, novel)
        mock_macro.assert_awaited_once_with(host, novel)


@pytest.mark.asyncio
async def test_process_novel_treats_legacy_planning_as_macro_planning():
    from domain.novel.entities.novel import AutopilotStatus, NovelStage

    host = MagicMock()
    host._is_still_running.return_value = True
    host.circuit_breaker = None
    novel = MagicMock()
    novel.novel_id.value = "n-1"
    novel.current_stage = NovelStage.PLANNING
    novel.autopilot_status = AutopilotStatus.RUNNING

    with patch(
        "engine.runtime.novel_lifecycle.run_macro_planning",
        new_callable=AsyncMock,
    ) as mock_macro:
        await process_novel(host, novel)
        assert novel.current_stage == NovelStage.MACRO_PLANNING
        host._save_novel_state.assert_called()
        mock_macro.assert_awaited_once_with(host, novel)


@pytest.mark.asyncio
async def test_run_macro_planning_stops_when_not_running():
    host = MagicMock()
    host._is_still_running.return_value = False
    novel = MagicMock()

    await run_macro_planning(host, novel)

    host.planning_service.generate_macro_plan.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("target_chapters", [None, 0, True, 1.5, "invalid"])
async def test_macro_planning_refuses_invalid_persisted_target_before_request(
    monkeypatch, target_chapters
):
    request = AsyncMock(
        return_value=SimpleNamespace(
            status="awaiting_pre_call_review",
            session_id="unexpected-request",
            operation="autopilot.macro.plan",
            node_key="planning-quick-macro",
            autopilot_pause_reason="awaiting_ai_review",
            payload={
                "session": SimpleNamespace(
                    policy=SimpleNamespace(value="AUTOPILOT_PAUSE")
                )
            },
        )
    )
    monkeypatch.setattr(
        "engine.runtime.macro_planning_delegate._request_macro_invocation",
        request,
    )
    host = SimpleNamespace(
        _is_still_running=lambda _novel: True,
        _update_shared_state=MagicMock(),
        _flush_novel=MagicMock(),
        planning_service=SimpleNamespace(
            apply_macro_plan_from_llm_result=AsyncMock(),
        ),
    )
    novel = SimpleNamespace(
        novel_id=SimpleNamespace(value="invalid-target-macro"),
        target_chapters=target_chapters,
        current_stage=NovelStage.MACRO_PLANNING,
        autopilot_status=AutopilotStatus.RUNNING,
    )

    await run_macro_planning(host, novel)

    assert novel.current_stage == NovelStage.PAUSED_FOR_REVIEW
    assert novel.autopilot_status == AutopilotStatus.STOPPED
    request.assert_not_awaited()
    host.planning_service.apply_macro_plan_from_llm_result.assert_not_awaited()
    host._update_shared_state.assert_any_call(
        "invalid-target-macro",
        current_stage=NovelStage.PAUSED_FOR_REVIEW.value,
        autopilot_pause_reason="target_chapters_required",
    )
    host._flush_novel.assert_called_once_with(novel)


@pytest.mark.asyncio
async def test_run_macro_planning_pauses_for_ai_invocation(monkeypatch):
    from domain.novel.entities.novel import NovelStage

    host = MagicMock()
    host._is_still_running.return_value = True
    host.planning_service.generate_macro_plan = AsyncMock()
    novel = MagicMock()
    novel.novel_id.value = "n-1"
    novel.target_chapters = 12
    novel.auto_approve_mode = False

    monkeypatch.setattr(
        "engine.runtime.macro_planning_delegate._read_shared_state",
        lambda _novel_id: {},
    )
    monkeypatch.setattr(
        "engine.runtime.macro_planning_delegate._request_macro_invocation",
        AsyncMock(
            return_value=SimpleNamespace(
                status="awaiting_pre_call_review",
                session_id="session-1",
                operation="autopilot.macro.plan",
                node_key="planning-quick-macro",
                autopilot_pause_reason="awaiting_ai_review",
                payload={
                    "session": SimpleNamespace(policy=SimpleNamespace(value="AUTOPILOT_PAUSE")),
                },
            )
        ),
    )

    await run_macro_planning(host, novel)

    host.planning_service.generate_macro_plan.assert_not_called()
    assert novel.current_stage == NovelStage.PAUSED_FOR_REVIEW
    host._update_shared_state.assert_any_call(
        "n-1",
        active_invocation_session_id="session-1",
        active_invocation_operation="autopilot.macro.plan",
        active_invocation_node_key="planning-quick-macro",
        active_invocation_status="awaiting_pre_call_review",
        active_invocation_policy="AUTOPILOT_PAUSE",
        has_active_invocation=True,
        requires_ai_review=True,
        autopilot_pause_reason="awaiting_ai_review",
        macro_structure_ready=False,
        writing_substep="macro_planning",
        writing_substep_label="宏观规划 · AI 请求面板",
    )


@pytest.mark.asyncio
async def test_run_act_planning_stops_when_not_running():
    host = MagicMock()
    host._is_still_running.return_value = False
    novel = MagicMock()

    await run_act_planning(host, novel)

    host.story_node_repo.get_by_novel.assert_not_called()


@pytest.mark.asyncio
async def test_run_act_planning_pauses_for_ai_invocation(monkeypatch):
    from domain.novel.entities.novel import NovelStage

    host = MagicMock()
    host._is_still_running.return_value = True
    host.planning_service.plan_act_chapters = AsyncMock()
    host.story_node_repo.get_children_sync.return_value = []

    target_act = MagicMock()
    target_act.id = "act-1"
    target_act.number = 1
    target_act.node_type.value = "act"
    target_act.suggested_chapter_count = 5
    target_act.title = "第一幕"
    target_act.description = "开端"

    host.story_node_repo.get_by_novel = AsyncMock(return_value=[target_act])

    novel = MagicMock()
    novel.novel_id.value = "n-1"
    novel.target_chapters = 12
    novel.current_act = 0
    novel.auto_approve_mode = False

    monkeypatch.setattr(
        "engine.runtime.act_planning_delegate._read_shared_state",
        lambda _novel_id: {},
    )
    monkeypatch.setattr(
        "engine.runtime.act_planning_delegate._request_act_invocation",
        AsyncMock(
            return_value=SimpleNamespace(
                status="awaiting_pre_call_review",
                session_id="session-1",
                operation="autopilot.act.plan",
                node_key="planning-act",
                autopilot_pause_reason="awaiting_ai_review",
                payload={
                    "session": SimpleNamespace(policy=SimpleNamespace(value="AUTOPILOT_PAUSE")),
                },
            )
        ),
    )

    await run_act_planning(host, novel)

    host.planning_service.plan_act_chapters.assert_not_called()
    assert novel.current_stage == NovelStage.PAUSED_FOR_REVIEW
    host._update_shared_state.assert_any_call(
        "n-1",
        active_invocation_session_id="session-1",
        active_invocation_operation="autopilot.act.plan",
        active_invocation_node_key="planning-act",
        active_invocation_status="awaiting_pre_call_review",
        active_invocation_policy="AUTOPILOT_PAUSE",
        has_active_invocation=True,
        requires_ai_review=True,
        autopilot_pause_reason="awaiting_ai_review",
    )


@pytest.mark.asyncio
async def test_run_writing_blocks_story_pipeline_before_candidate_run_exists():
    host = MagicMock()
    host.use_story_pipeline_for_writing = True
    host.chapter_repository = SimpleNamespace(
        db=SimpleNamespace(fetch_one=MagicMock(return_value=None))
    )
    novel = MagicMock()

    with patch(
        "engine.runtime.writing_delegate.run_story_pipeline_writing",
        new_callable=AsyncMock,
    ) as mock_pipeline, patch(
        "engine.runtime.legacy_writing_delegate.run_legacy_writing",
        new_callable=AsyncMock,
    ) as mock_legacy:
        await run_writing(host, novel)
        mock_pipeline.assert_not_awaited()
        mock_legacy.assert_not_awaited()


@pytest.mark.asyncio
async def test_run_writing_blocks_legacy_pipeline_before_candidate_run_exists():
    host = MagicMock()
    host.use_story_pipeline_for_writing = False
    host.chapter_repository = SimpleNamespace(
        db=SimpleNamespace(fetch_one=MagicMock(return_value=None))
    )
    novel = MagicMock()

    with patch(
        "engine.runtime.writing_delegate.run_story_pipeline_writing",
        new_callable=AsyncMock,
    ) as mock_pipeline, patch(
        "engine.runtime.legacy_writing_delegate.run_legacy_writing",
        new_callable=AsyncMock,
    ) as mock_legacy:
        await run_writing(host, novel)
        mock_legacy.assert_not_awaited()
        mock_pipeline.assert_not_awaited()


@pytest.mark.asyncio
async def test_process_novel_routes_writing_via_run_writing():
    from domain.novel.entities.novel import AutopilotStatus, NovelStage

    host = MagicMock()
    host._is_still_running.return_value = True
    host.circuit_breaker = None
    novel = MagicMock()
    novel.novel_id.value = "n-1"
    novel.current_stage = NovelStage.WRITING
    novel.autopilot_status = AutopilotStatus.RUNNING

    with patch(
        "engine.runtime.writing_delegate.run_writing",
        new_callable=AsyncMock,
    ) as mock_writing:
        await process_novel(host, novel)
        mock_writing.assert_awaited_once_with(host, novel)


@pytest.mark.asyncio
async def test_process_novel_auto_mode_keeps_canonical_failure_paused():
    from domain.novel.entities.novel import AutopilotStatus, NovelStage

    host = MagicMock()
    host._is_still_running.return_value = True
    host._latest_completed_chapter_number.return_value = 1
    host._is_chapter_narrative_ready.return_value = False
    host.circuit_breaker = None
    novel = MagicMock()
    novel.novel_id.value = "n-1"
    novel.current_stage = NovelStage.PAUSED_FOR_REVIEW
    novel.autopilot_status = AutopilotStatus.RUNNING
    novel.auto_approve_mode = True

    await process_novel(host, novel)

    assert novel.current_stage == NovelStage.PAUSED_FOR_REVIEW
    assert novel.last_audit_narrative_ok is False
    host._update_shared_state.assert_any_call(
        "n-1",
        current_stage="paused_for_review",
        last_audit_narrative_ok=False,
        autopilot_pause_reason="canonical_aftermath_not_ready",
    )
