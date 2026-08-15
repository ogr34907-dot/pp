from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from application.engine.services.generation_start_preflight import (
    GenerationStartPreflight,
    GenerationStartPreflightError,
)


@pytest.mark.parametrize("target_chapters", [None, 0, True, 1.5, "invalid"])
def test_invalid_persisted_target_stops_before_candidate_and_outline_work(target_chapters):
    novel_cursor = Mock()
    novel_cursor.fetchone.return_value = {
        "autopilot_recovery_reason": "",
        "target_chapters": target_chapters,
    }
    run_cursor = Mock()
    run_cursor.fetchone.return_value = None
    connection = Mock()
    connection.execute.side_effect = [novel_cursor, run_cursor]
    candidate_repository = SimpleNamespace(
        assert_formal_history_is_proven=Mock(
            side_effect=RuntimeError("candidate repository must not be called")
        ),
        formal_chapter_head=Mock(),
        formal_slot_is_available=Mock(),
    )
    outline_service = Mock()
    preflight = GenerationStartPreflight(
        SimpleNamespace(get_connection=Mock(return_value=connection)),
        candidate_repository,
        outline_service,
    )

    with pytest.raises(GenerationStartPreflightError, match="target_chapters_required"):
        preflight.ensure_startable("novel-1")

    assert connection.execute.call_count == 1
    candidate_repository.assert_formal_history_is_proven.assert_not_called()
    candidate_repository.formal_chapter_head.assert_not_called()
    candidate_repository.formal_slot_is_available.assert_not_called()
    outline_service.next_published_chapter_context.assert_not_called()


def test_waiting_planning_is_a_durable_start_gate():
    novel_cursor = Mock()
    novel_cursor.fetchone.return_value = {
        "autopilot_recovery_reason": "",
        "target_chapters": 20,
    }
    run_cursor = Mock()
    run_cursor.fetchone.return_value = {
        "state": "waiting_planning",
        "canonical_sync_status": "ready",
        "generation_epoch": 3,
    }
    connection = Mock()
    connection.execute.side_effect = [novel_cursor, run_cursor]
    candidate_repository = SimpleNamespace(
        assert_formal_history_is_proven=Mock(),
        formal_chapter_head=Mock(),
        formal_slot_is_available=Mock(),
    )
    preflight = GenerationStartPreflight(
        SimpleNamespace(get_connection=Mock(return_value=connection)),
        candidate_repository,
        Mock(),
    )

    with pytest.raises(GenerationStartPreflightError, match="outline_expansion_required"):
        preflight.ensure_startable("novel-1")

    candidate_repository.assert_formal_history_is_proven.assert_not_called()
