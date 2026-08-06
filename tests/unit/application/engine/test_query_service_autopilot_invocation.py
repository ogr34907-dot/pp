import json

from application.engine.services.query_service import QueryService
from application.engine.services.shared_state_repository import NovelState, SharedStateRepository


def test_query_service_exposes_configured_protection_limit_from_shared_state():
    """STATUS-003: cockpit status must render the configured safety cap, not 9999."""
    repo = SharedStateRepository(shared_dict={})
    repo.set_novel_state(
        "novel-1",
        NovelState(
            novel_id="novel-1",
            title="Demo",
            autopilot_status="running",
            current_stage="writing",
            current_act=0,
            current_chapter_in_act=0,
            current_beat_index=0,
            current_auto_chapters=0,
            max_auto_chapters=1,
            target_chapters=500,
            target_words_per_chapter=2000,
            consecutive_error_count=0,
            last_chapter_tension=0,
            auto_approve_mode=False,
            needs_review=False,
        ),
    )

    status = QueryService(repo).get_novel_status_dict("novel-1")

    assert status is not None
    assert status["max_auto_chapters"] == 1


def test_query_service_status_dict_exposes_active_invocation_flag():
    repo = SharedStateRepository(shared_dict={})
    repo.set_novel_state(
        "novel-1",
        NovelState(
            novel_id="novel-1",
            title="Demo",
            autopilot_status="running",
            current_stage="writing",
            current_act=None,
            current_chapter_in_act=None,
            current_beat_index=0,
            current_auto_chapters=0,
            target_chapters=10,
            target_words_per_chapter=2500,
            consecutive_error_count=0,
            last_chapter_tension=0,
            auto_approve_mode=False,
            needs_review=False,
            active_invocation_session_id="session-1",
            active_invocation_operation="autopilot.prose.from_script",
            active_invocation_node_key="autopilot-stream-beat",
            active_invocation_status="generating",
            active_invocation_policy="AUTOPILOT_PAUSE",
            has_active_invocation=True,
            requires_ai_review=False,
            autopilot_pause_reason="",
        ),
    )

    status = QueryService(repo).get_novel_status_dict("novel-1")

    assert status is not None
    assert status["active_invocation_session_id"] == "session-1"
    assert status["has_active_invocation"] is True
    assert status["active_invocation_status"] == "generating"


def test_query_service_keeps_completed_invocation_session_without_active_flag():
    repo = SharedStateRepository(shared_dict={})
    repo.set_novel_state(
        "novel-1",
        NovelState(
            novel_id="novel-1",
            title="Demo",
            autopilot_status="running",
            current_stage="writing",
            current_act=None,
            current_chapter_in_act=None,
            current_beat_index=0,
            current_auto_chapters=0,
            target_chapters=10,
            target_words_per_chapter=2500,
            consecutive_error_count=0,
            last_chapter_tension=0,
            auto_approve_mode=True,
            needs_review=False,
            active_invocation_session_id="session-1",
            active_invocation_operation="autopilot.prose.from_script",
            active_invocation_node_key="autopilot-stream-beat",
            active_invocation_status="completed",
            active_invocation_policy="DIRECT",
            has_active_invocation=False,
            requires_ai_review=False,
            autopilot_pause_reason="",
        ),
    )

    status = QueryService(repo).get_novel_status_dict("novel-1")

    assert status is not None
    assert status["active_invocation_session_id"] == "session-1"
    assert status["has_active_invocation"] is False
    assert status["requires_ai_review"] is False


def test_query_service_derives_review_gate_from_stage_even_if_flag_is_stale():
    repo = SharedStateRepository(shared_dict={})
    repo.set_novel_state(
        "novel-1",
        NovelState(
            novel_id="novel-1",
            title="Demo",
            autopilot_status="running",
            current_stage="paused_for_review",
            current_act=0,
            current_chapter_in_act=0,
            current_beat_index=0,
            current_auto_chapters=0,
            target_chapters=500,
            target_words_per_chapter=2000,
            consecutive_error_count=0,
            last_chapter_tension=0,
            auto_approve_mode=False,
            needs_review=False,
            active_invocation_session_id="session-1",
            active_invocation_operation="autopilot.macro.plan",
            active_invocation_node_key="planning-quick-macro",
            active_invocation_status="generating",
            active_invocation_policy="AUTOPILOT_PAUSE",
            has_active_invocation=True,
            requires_ai_review=False,
            autopilot_pause_reason="",
        ),
    )

    status = QueryService(repo).get_novel_status_dict("novel-1")

    assert status is not None
    assert status["needs_review"] is True
    assert status["review_gate"]["type"] == "macro_plan"
    assert status["review_gate"]["status"] == "awaiting_ai_review"
    assert status["review_gate"]["can_resume"] is False


def test_query_service_blocks_resume_while_pending_macro_plan_is_not_persisted():
    repo = SharedStateRepository(shared_dict={})
    repo.set_novel_state(
        "novel-1",
        NovelState(
            novel_id="novel-1",
            title="Demo",
            autopilot_status="running",
            current_stage="macro_planning",
            current_act=0,
            current_chapter_in_act=0,
            current_beat_index=0,
            current_auto_chapters=0,
            target_chapters=500,
            target_words_per_chapter=2000,
            consecutive_error_count=0,
            last_chapter_tension=0,
            auto_approve_mode=False,
            needs_review=False,
        ),
    )
    repo.merge_raw_state(
        "novel-1",
        autopilot_pending_macro_plan={"success": True, "structure": [{"title": "第一部"}]},
        autopilot_pending_macro_target_chapters=500,
        macro_structure_ready=False,
    )

    status = QueryService(repo).get_novel_status_dict("novel-1")

    assert status is not None
    assert status["review_gate"]["type"] == "macro_plan"
    assert status["review_gate"]["status"] == "persisting"
    assert status["review_gate"]["can_resume"] is False


def test_query_service_marks_initial_macro_review_ready_after_structure_persisted():
    repo = SharedStateRepository(shared_dict={})
    repo.set_novel_state(
        "novel-1",
        NovelState(
            novel_id="novel-1",
            title="Demo",
            autopilot_status="running",
            current_stage="paused_for_review",
            current_act=0,
            current_chapter_in_act=0,
            current_beat_index=0,
            current_auto_chapters=0,
            target_chapters=500,
            target_words_per_chapter=2000,
            consecutive_error_count=0,
            last_chapter_tension=0,
            auto_approve_mode=False,
            needs_review=False,
        ),
    )
    repo.merge_raw_state("novel-1", macro_structure_ready=True)

    status = QueryService(repo).get_novel_status_dict("novel-1")

    assert status is not None
    assert status["needs_review"] is True
    assert status["review_gate"]["type"] == "macro_plan"
    assert status["review_gate"]["status"] == "ready"
    assert status["review_gate"]["can_resume"] is True


def test_query_service_does_not_hydrate_pending_invocation_by_partial_novel_id(monkeypatch):
    class _FakeDatabase:
        def fetch_all(self, *_args, **_kwargs):
            return [
                {
                    "id": "session-wrong",
                    "operation": "autopilot.macro.plan",
                    "node_key": "planning-quick-macro",
                    "policy": "AUTOPILOT_PAUSE",
                    "status": "awaiting_pre_call_review",
                    "context_json": json.dumps({"novel_id": "novel-10"}),
                    "metadata_json": "{}",
                }
            ]

    monkeypatch.setattr("application.paths.get_db_path", lambda: "test.db")
    monkeypatch.setattr(
        "infrastructure.persistence.database.connection.get_database",
        lambda _path=None: _FakeDatabase(),
    )

    repo = SharedStateRepository(shared_dict={})
    repo.set_novel_state(
        "novel-1",
        NovelState(
            novel_id="novel-1",
            title="Demo",
            autopilot_status="running",
            current_stage="macro_planning",
            current_act=0,
            current_chapter_in_act=0,
            current_beat_index=0,
            current_auto_chapters=0,
            target_chapters=500,
            target_words_per_chapter=2000,
            consecutive_error_count=0,
            last_chapter_tension=0,
            auto_approve_mode=False,
            needs_review=False,
        ),
    )

    status = QueryService(repo).get_novel_status_dict("novel-1")

    assert status is not None
    assert status["active_invocation_session_id"] == ""
    assert "review_gate" not in status


def test_query_service_does_not_invent_global_chapter_number_from_act_position():
    """STATUS-001: act capacity is structural, not a fixed five-chapter constant."""
    repo = SharedStateRepository(shared_dict={})
    repo.set_novel_state(
        "novel-1",
        NovelState(
            novel_id="novel-1",
            title="Demo",
            autopilot_status="running",
            current_stage="writing",
            current_act=1,
            current_chapter_in_act=2,
            current_beat_index=0,
            current_auto_chapters=0,
            target_chapters=20,
            target_words_per_chapter=2500,
            consecutive_error_count=0,
            last_chapter_tension=0,
            auto_approve_mode=False,
            needs_review=False,
        ),
    )

    status = QueryService(repo).get_novel_status_dict("novel-1")

    assert status is not None
    assert status["current_chapter_number"] is None


def test_query_service_raw_status_does_not_invent_global_chapter_number():
    """STATUS-001 applies equally to the legacy raw-state fallback."""
    repo = SharedStateRepository(shared_dict={})
    response = QueryService(repo)._build_status_from_raw(
        "novel-1",
        {
            "title": "Demo",
            "autopilot_status": "running",
            "current_stage": "writing",
            "current_act": 1,
            "current_chapter_in_act": 2,
            "target_chapters": 20,
            "target_words_per_chapter": 2500,
        },
    )

    assert response.current_chapter_number is None


def test_query_service_exposes_the_durable_manual_pause_reason():
    """AUTOPILOT-002: the cockpit can render an explicit resume control."""
    repo = SharedStateRepository(shared_dict={})
    repo.set_novel_state(
        "novel-1",
        NovelState(
            novel_id="novel-1",
            title="Demo",
            autopilot_status="stopped",
            current_stage="writing",
            current_act=1,
            current_chapter_in_act=2,
            current_beat_index=0,
            current_auto_chapters=0,
            target_chapters=20,
            target_words_per_chapter=2500,
            consecutive_error_count=0,
            last_chapter_tension=0,
            auto_approve_mode=False,
            needs_review=False,
        ),
    )
    repo.merge_raw_state("novel-1", autopilot_recovery_reason="manual_pause")

    status = QueryService(repo).get_novel_status_dict("novel-1")

    assert status is not None
    assert status["autopilot_recovery_reason"] == "manual_pause"
