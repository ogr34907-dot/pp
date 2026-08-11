"""Candidate-generation state overrides stale legacy autopilot presentation."""

from application.engine.services.query_service import _overlay_generation_authority


def test_stopped_generation_run_never_leaks_a_stale_writing_status():
    payload = {
        "autopilot_status": "running",
        "current_stage": "writing",
        "current_chapter_number": 9,
    }
    run = {
        "novel_id": "novel-1",
        "run_mode": "chapter_review",
        "state": "stopped",
        "generation_epoch": 4,
        "target_chapters": 80,
        "current_formal_chapter": 40,
        "current_candidate_id": None,
        "current_candidate_chapter": None,
        "canonical_sync_status": "ready",
        "next_action": "idle",
        "last_error": "",
        "max_pending_candidates": 1,
        "prefetch": 0,
    }

    result = _overlay_generation_authority(payload, run, None)

    assert result["autopilot_status"] == "stopped"
    assert result["current_stage"] == "stopped"
    assert result["generation"]["state"] == "stopped"
    assert result["current_chapter_number"] == 41


def test_waiting_review_is_exposed_as_authoritative_candidate_state_not_writing():
    payload = {"autopilot_status": "running", "current_stage": "writing"}
    run = {
        "novel_id": "novel-1",
        "run_mode": "chapter_review",
        "state": "waiting_review",
        "generation_epoch": 1,
        "target_chapters": 10,
        "current_formal_chapter": 2,
        "current_candidate_id": "candidate-3",
        "current_candidate_chapter": 3,
        "canonical_sync_status": "ready",
        "next_action": "author_review_candidate",
        "last_error": "",
        "max_pending_candidates": 1,
        "prefetch": 0,
    }
    candidate = {"id": "candidate-3", "chapter_number": 3, "status": "awaiting_review"}

    result = _overlay_generation_authority(payload, run, candidate)

    assert result["autopilot_status"] == "paused_for_review"
    assert result["current_stage"] == "waiting_review"
    assert result["generation"]["candidate"]["status"] == "awaiting_review"
