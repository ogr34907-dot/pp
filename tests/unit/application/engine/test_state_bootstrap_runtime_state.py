from application.engine.services.shared_state_repository import SharedStateRepository
from application.engine.services.state_bootstrap import StateBootstrap


def test_state_bootstrap_preserves_autopilot_recovery_reason(monkeypatch):
    """AUTOPILOT-002: a restart keeps a manual pause visible and resumable."""
    shared = SharedStateRepository(shared_dict={})
    bootstrap = StateBootstrap(shared_state=shared)
    monkeypatch.setattr(bootstrap, "_macro_structure_ready", lambda _novel_id: False)

    bootstrap._load_novel_state(
        {
            "id": "novel-1",
            "title": "Demo",
            "autopilot_status": "stopped",
            "autopilot_recovery_reason": "manual_pause",
            "current_stage": "writing",
            "current_act": 1,
            "current_chapter_in_act": 2,
            "current_beat_index": 0,
            "current_auto_chapters": 0,
            "target_chapters": 20,
            "target_words_per_chapter": 2500,
            "consecutive_error_count": 0,
            "last_chapter_tension": 0,
            "auto_approve_mode": False,
            "needs_review": False,
        }
    )

    raw = shared.get_raw_state("novel-1")

    assert raw["autopilot_recovery_reason"] == "manual_pause"


def test_state_bootstrap_preserves_configured_protection_limit(monkeypatch):
    """STATUS-003: restart must not replace a configured safety cap with 9999."""
    shared = SharedStateRepository(shared_dict={})
    bootstrap = StateBootstrap(shared_state=shared)
    monkeypatch.setattr(bootstrap, "_macro_structure_ready", lambda _novel_id: False)

    bootstrap._load_novel_state(
        {
            "id": "novel-1",
            "title": "Demo",
            "autopilot_status": "stopped",
            "autopilot_recovery_reason": "manual_pause",
            "current_stage": "writing",
            "current_act": 1,
            "current_chapter_in_act": 2,
            "current_beat_index": 0,
            "current_auto_chapters": 0,
            "max_auto_chapters": 1,
            "target_chapters": 500,
            "target_words_per_chapter": 2000,
            "consecutive_error_count": 0,
            "last_chapter_tension": 0,
            "auto_approve_mode": False,
            "needs_review": False,
        }
    )

    raw = shared.get_raw_state("novel-1")

    assert raw["max_auto_chapters"] == 1
