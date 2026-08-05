import json

import pytest

from interfaces.api.v1.engine import autopilot_routes


@pytest.fixture(autouse=True)
def clear_autopilot_sse_replay_state():
    for attribute in (
        "_autopilot_sse_event_history",
        "_autopilot_sse_event_sequences",
        "_autopilot_sse_event_fingerprints",
    ):
        state = getattr(autopilot_routes, attribute, None)
        if state is not None:
            state.clear()
    yield
    for attribute in (
        "_autopilot_sse_event_history",
        "_autopilot_sse_event_sequences",
        "_autopilot_sse_event_fingerprints",
    ):
        state = getattr(autopilot_routes, attribute, None)
        if state is not None:
            state.clear()


def _event_id(frame: str) -> str:
    return next(line.removeprefix("id: ") for line in frame.splitlines() if line.startswith("id: "))


def _payload(frame: str) -> dict:
    return json.loads(next(line.removeprefix("data: ") for line in frame.splitlines() if line.startswith("data: ")))


def _stage_change(to_stage: str) -> dict:
    return {
        "type": "stage_change",
        "message": f"阶段变更：写作 → {to_stage}",
        "timestamp": "2026-08-05T10:00:00+00:00",
        "metadata": {
            "from_stage": "writing",
            "to_stage": to_stage,
        },
    }


def test_autopilot_general_sse_events_have_stable_ids_and_replay_after_cursor():
    """SSE-001: reconnectable cockpit events retain an ID and replay only the missing suffix."""
    first_frame = autopilot_routes._format_autopilot_sse_event(
        "novel-autopilot",
        _stage_change("auditing"),
    )
    duplicate_frame = autopilot_routes._format_autopilot_sse_event(
        "novel-autopilot",
        _stage_change("auditing"),
    )
    second_frame = autopilot_routes._format_autopilot_sse_event(
        "novel-autopilot",
        _stage_change("paused_for_review"),
    )

    first_event_id = _event_id(first_frame)
    assert first_event_id == _event_id(duplicate_frame)
    assert _payload(first_frame)["event_id"] == first_event_id
    assert _payload(second_frame)["event_id"] != first_event_id

    replayed = autopilot_routes._autopilot_events_after_cursor(
        "novel-autopilot",
        first_event_id,
    )
    assert [event["metadata"]["to_stage"] for event in replayed] == ["paused_for_review"]


def test_autopilot_log_lines_have_standard_ids_without_entering_general_event_history():
    """SSE-001: log replay remains owned by after_seq while frames are still client-deduplicable."""
    frame = autopilot_routes._format_autopilot_sse_event(
        "novel-log",
        {
            "type": "log_line",
            "message": "writer ready",
            "timestamp": "2026-08-05T10:00:00+00:00",
            "metadata": {"seq": 42, "level": "INFO", "logger": "test"},
        },
    )

    event_id = _event_id(frame)
    assert _payload(frame)["event_id"] == event_id
    assert autopilot_routes._autopilot_events_after_cursor("novel-log", event_id) == []
