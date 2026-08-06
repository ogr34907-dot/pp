from types import SimpleNamespace

import pytest

from interfaces.api.v1.engine import autopilot_routes


@pytest.mark.asyncio
async def test_start_publishes_selected_protection_limit_to_shared_state(monkeypatch):
    """AUTOPILOT-UI-003: the daemon must receive the user-selected safety cap."""
    shared_updates = []
    start_signals = []

    monkeypatch.setattr(
        autopilot_routes,
        "get_autopilot_runtime_settings",
        lambda: SimpleNamespace(db_persist_timeout_seconds=1),
    )
    monkeypatch.setattr(
        autopilot_routes,
        "_get_shared_state_for_novel",
        lambda _novel_id: {
            "_updated_at": 1,
            "current_stage": "writing",
            "current_act": 0,
            "current_chapter_in_act": 0,
            "current_beat_index": 0,
            "target_chapters": 500,
            "target_words_per_chapter": 2000,
        },
    )
    monkeypatch.setattr(
        "interfaces.runtime_state.update_shared_novel_state",
        lambda novel_id, **fields: shared_updates.append((novel_id, fields)),
    )
    monkeypatch.setattr(
        autopilot_routes,
        "_persist_autopilot_running_sync",
        lambda _novel_id, **_kwargs: {"decision": None, "run_epoch": 7},
    )
    monkeypatch.setattr(
        "application.engine.services.novel_stop_signal.publish_start_signal",
        lambda novel_id: start_signals.append(novel_id),
    )

    response = await autopilot_routes.start_autopilot(
        "novel-1",
        autopilot_routes.StartRequest(
            max_auto_chapters=1,
            target_chapters=500,
            target_words_per_chapter=2000,
        ),
    )

    assert response["success"] is True
    assert shared_updates[0][0] == "novel-1"
    assert shared_updates[0][1]["max_auto_chapters"] == 1
    assert start_signals == ["novel-1"]


@pytest.mark.asyncio
async def test_pause_publishes_a_resumable_manual_pause_intent(monkeypatch):
    """AUTOPILOT-002: pause is distinct from destructive stop/cleanup."""
    shared_updates = []
    signals = []
    persisted = []

    monkeypatch.setattr(
        autopilot_routes,
        "_get_shared_state_for_novel",
        lambda _novel_id: {"autopilot_status": "running"},
    )
    monkeypatch.setattr(
        "interfaces.runtime_state.update_shared_novel_state",
        lambda novel_id, **fields: shared_updates.append((novel_id, fields)),
    )
    monkeypatch.setattr(
        "application.engine.services.novel_stop_signal.publish_stop_signal",
        lambda novel_id: signals.append(novel_id),
    )
    monkeypatch.setattr(
        autopilot_routes,
        "_persist_autopilot_stop_intent_sync",
        lambda novel_id, **kwargs: persisted.append((novel_id, kwargs)),
        raising=False,
    )

    response = await autopilot_routes.pause_autopilot("novel-1")

    assert response["success"] is True
    assert signals == ["novel-1"]
    assert persisted == [
        (
            "novel-1",
            {"recovery_reason": "manual_pause", "cleanup_transient": False},
        )
    ]
    assert shared_updates[-1] == (
        "novel-1",
        {
            "autopilot_status": "stopped",
            "autopilot_pause_reason": "manual_pause",
            "autopilot_recovery_reason": "manual_pause",
        },
    )


@pytest.mark.asyncio
async def test_terminate_publishes_destructive_stop_intent(monkeypatch):
    """AUTOPILOT-002: terminate retains the existing stop-and-cleanup behavior."""
    shared_updates = []
    signals = []
    persisted = []

    monkeypatch.setattr(
        autopilot_routes,
        "_get_shared_state_for_novel",
        lambda _novel_id: {"autopilot_status": "running"},
    )
    monkeypatch.setattr(
        "interfaces.runtime_state.update_shared_novel_state",
        lambda novel_id, **fields: shared_updates.append((novel_id, fields)),
    )
    monkeypatch.setattr(
        "application.engine.services.novel_stop_signal.publish_stop_signal",
        lambda novel_id: signals.append(novel_id),
    )
    monkeypatch.setattr(
        autopilot_routes,
        "_persist_autopilot_stop_intent_sync",
        lambda novel_id, **kwargs: persisted.append((novel_id, kwargs)),
        raising=False,
    )

    response = await autopilot_routes.terminate_autopilot("novel-1")

    assert response["success"] is True
    assert signals == ["novel-1"]
    assert persisted == [
        (
            "novel-1",
            {"recovery_reason": "manual_terminate", "cleanup_transient": True},
        )
    ]
    assert shared_updates[-1] == (
        "novel-1",
        {
            "autopilot_status": "stopped",
            "autopilot_pause_reason": "manual_terminate",
            "autopilot_recovery_reason": "manual_terminate",
        },
    )


@pytest.mark.asyncio
async def test_resume_accepts_a_manually_paused_writing_stage(monkeypatch):
    """AUTOPILOT-002: manual pause can resume the same stable stage."""
    shared_updates = []
    start_signals = []
    persisted = []
    shared = {
        "_updated_at": 1,
        "autopilot_status": "stopped",
        "autopilot_recovery_reason": "manual_pause",
        "current_stage": "writing",
        "current_act": 1,
    }

    class _Repo:
        def get_by_id(self, _novel_id):
            return SimpleNamespace(
                max_auto_chapters=9999,
                target_chapters=20,
                target_words_per_chapter=2500,
            )

    monkeypatch.setattr(autopilot_routes, "_get_shared_state_for_novel", lambda _novel_id: shared)
    monkeypatch.setattr(autopilot_routes, "_canonical_resume_block_reason", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(autopilot_routes, "get_novel_repository", lambda: _Repo())
    monkeypatch.setattr(
        autopilot_routes,
        "_persist_autopilot_resume_sync",
        lambda novel_id, **kwargs: persisted.append((novel_id, kwargs)),
    )
    monkeypatch.setattr(
        "interfaces.runtime_state.update_shared_novel_state",
        lambda novel_id, **fields: shared_updates.append((novel_id, fields)),
    )
    monkeypatch.setattr(
        "application.engine.services.novel_stop_signal.publish_start_signal",
        lambda novel_id: start_signals.append(novel_id),
    )

    response = await autopilot_routes.resume_from_review("novel-1")

    assert response["success"] is True
    assert response["current_stage"] == "writing"
    assert persisted == [
        (
            "novel-1",
            {
                "next_stage": "writing",
                "current_act": 1,
                "max_auto_chapters": 9999,
                "target_chapters": 20,
                "target_words_per_chapter": 2500,
            },
        )
    ]
    assert shared_updates[-1] == (
        "novel-1",
        {
            "autopilot_status": "running",
            "current_stage": "writing",
            "current_act": 1,
            "autopilot_pause_reason": "",
            "autopilot_recovery_reason": "",
        },
    )
    assert start_signals == ["novel-1"]
