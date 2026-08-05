import asyncio
import json

import pytest

from interfaces.api.v1.engine.dag import dag_routes
from interfaces.api.v1.engine.dag.dag_runtime_settings import DAGRuntimeSettings


@pytest.fixture(autouse=True)
def clear_dag_sse_state():
    for attribute in (
        "_sse_subscribers",
        "_sse_event_history",
        "_sse_event_sequences",
        "_sse_projection_snapshots",
    ):
        state = getattr(dag_routes, attribute, None)
        if state is not None:
            state.clear()
    yield
    for attribute in (
        "_sse_subscribers",
        "_sse_event_history",
        "_sse_event_sequences",
        "_sse_projection_snapshots",
    ):
        state = getattr(dag_routes, attribute, None)
        if state is not None:
            state.clear()


def _node_status_event(novel_id: str, node_id: str) -> dict:
    return {
        "type": "node_status_change",
        "novel_id": novel_id,
        "node_id": node_id,
        "status": "running",
        "timestamp": "2026-08-05T10:00:00+00:00",
    }


def _event_id(frame: str) -> str:
    return next(line.removeprefix("id: ") for line in frame.splitlines() if line.startswith("id: "))


def _event_payload(frame: str) -> dict:
    return json.loads(next(line.removeprefix("data: ") for line in frame.splitlines() if line.startswith("data: ")))


@pytest.mark.asyncio
async def test_dag_sse_assigns_a_standard_event_id_to_live_events():
    """SSE-001: a reconnectable DAG event must carry a standard SSE id."""
    response = await dag_routes.dag_event_stream("novel-sse")
    iterator = response.body_iterator

    try:
        await anext(iterator)  # connected acknowledgement
        dag_routes.publish_sse_event(
            "novel-sse",
            _node_status_event("novel-sse", "compose"),
        )

        frame = await asyncio.wait_for(anext(iterator), timeout=1)

        assert frame.startswith("id: ")
        assert "event: node_status_change" in frame
        assert _event_payload(frame)["event_id"] == _event_id(frame)
    finally:
        await iterator.aclose()


@pytest.mark.asyncio
async def test_dag_sse_replays_events_after_the_client_cursor_in_order():
    """SSE-001: a manual EventSource reconnect receives only the missing DAG events."""
    response = await dag_routes.dag_event_stream("novel-replay")
    iterator = response.body_iterator

    try:
        await anext(iterator)
        dag_routes.publish_sse_event("novel-replay", _node_status_event("novel-replay", "first"))
        first_frame = await asyncio.wait_for(anext(iterator), timeout=1)
        first_event_id = _event_id(first_frame)

        dag_routes.publish_sse_event("novel-replay", _node_status_event("novel-replay", "second"))
        await asyncio.wait_for(anext(iterator), timeout=1)
        dag_routes.publish_sse_event("novel-replay", _node_status_event("novel-replay", "third"))
        await asyncio.wait_for(anext(iterator), timeout=1)
    finally:
        await iterator.aclose()

    replay_response = await dag_routes.dag_event_stream(
        "novel-replay",
        after_event_id=first_event_id,
    )
    replay_iterator = replay_response.body_iterator
    try:
        await anext(replay_iterator)
        replayed = [
            await asyncio.wait_for(anext(replay_iterator), timeout=1),
            await asyncio.wait_for(anext(replay_iterator), timeout=1),
        ]
        assert [_event_payload(frame)["node_id"] for frame in replayed] == ["second", "third"]
        assert all(frame.startswith("id: ") for frame in replayed)
    finally:
        await replay_iterator.aclose()


@pytest.mark.asyncio
async def test_dag_sse_honors_the_standard_last_event_id_header_value():
    """SSE-001: native EventSource reconnects can resume through Last-Event-ID."""
    response = await dag_routes.dag_event_stream("novel-header")
    iterator = response.body_iterator
    try:
        await anext(iterator)
        dag_routes.publish_sse_event("novel-header", _node_status_event("novel-header", "first"))
        first_event_id = _event_id(await asyncio.wait_for(anext(iterator), timeout=1))
    finally:
        await iterator.aclose()

    dag_routes.publish_sse_event("novel-header", _node_status_event("novel-header", "second"))
    replay_response = await dag_routes.dag_event_stream(
        "novel-header",
        last_event_id=first_event_id,
    )
    replay_iterator = replay_response.body_iterator
    try:
        await anext(replay_iterator)
        replayed = await asyncio.wait_for(anext(replay_iterator), timeout=1)
        assert _event_payload(replayed)["node_id"] == "second"
    finally:
        await replay_iterator.aclose()


@pytest.mark.asyncio
async def test_dag_sse_does_not_replay_current_process_history_for_an_unknown_epoch_cursor():
    """SSE-001: an old server cursor triggers client state reconciliation, not false replay."""
    dag_routes.publish_sse_event("novel-epoch", _node_status_event("novel-epoch", "current"))

    response = await dag_routes.dag_event_stream(
        "novel-epoch",
        after_event_id="retired-process:999",
    )
    iterator = response.body_iterator
    try:
        await anext(iterator)
        dag_routes.publish_sse_event("novel-epoch", _node_status_event("novel-epoch", "fresh"))
        next_frame = await asyncio.wait_for(anext(iterator), timeout=1)
        assert _event_payload(next_frame)["node_id"] == "fresh"
    finally:
        await iterator.aclose()


@pytest.mark.asyncio
async def test_dag_sse_does_not_replay_a_partial_history_after_cursor_eviction(monkeypatch):
    """SSE-001: once a cursor is evicted, the server never pretends retained tail events are complete."""
    monkeypatch.setattr(
        dag_routes,
        "get_dag_runtime_settings",
        lambda: DAGRuntimeSettings(sse_queue_size=2),
    )

    response = await dag_routes.dag_event_stream("novel-eviction")
    iterator = response.body_iterator
    try:
        await anext(iterator)
        dag_routes.publish_sse_event("novel-eviction", _node_status_event("novel-eviction", "first"))
        evicted_cursor = _event_id(await asyncio.wait_for(anext(iterator), timeout=1))
        dag_routes.publish_sse_event("novel-eviction", _node_status_event("novel-eviction", "second"))
        await asyncio.wait_for(anext(iterator), timeout=1)
        dag_routes.publish_sse_event("novel-eviction", _node_status_event("novel-eviction", "third"))
        await asyncio.wait_for(anext(iterator), timeout=1)
    finally:
        await iterator.aclose()

    replay_response = await dag_routes.dag_event_stream(
        "novel-eviction",
        after_event_id=evicted_cursor,
    )
    replay_iterator = replay_response.body_iterator
    try:
        await anext(replay_iterator)
        dag_routes.publish_sse_event("novel-eviction", _node_status_event("novel-eviction", "fresh"))
        next_frame = await asyncio.wait_for(anext(replay_iterator), timeout=1)
        assert _event_payload(next_frame)["node_id"] == "fresh"
    finally:
        await replay_iterator.aclose()
