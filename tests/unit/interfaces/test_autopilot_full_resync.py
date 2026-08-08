import json

import pytest
from fastapi import HTTPException

from interfaces.api.v1.engine import autopilot_routes


class _Database:
    def __init__(self, row):
        self.row = row

    def fetch_one(self, query, params=()):
        if "FROM novels" in query:
            return self.row
        return None


def _decode_frames(chunks):
    frames = []
    for chunk in chunks:
        if isinstance(chunk, bytes):
            chunk = chunk.decode()
        assert chunk.startswith("data: ")
        assert chunk.endswith("\n\n")
        frames.append(json.loads(chunk[6:-2]))
    return frames


@pytest.mark.asyncio
async def test_full_resync_route_streams_ordered_events_and_keeps_novel_paused(monkeypatch):
    database = _Database({"id": "novel-1", "autopilot_recovery_reason": ""})
    pause_calls = []
    resume_calls = []

    async def fake_pause(*args, **kwargs):
        pause_calls.append((args, kwargs))
        return {"success": True}

    async def fake_service(*, novel_id, database, aftermath_pipeline, emit):
        await emit({"type": "started", "run_id": "run-1", "total": 2, "pending_chapters": 2})
        await emit({"type": "chapter", "run_id": "run-1", "chapter_number": 1, "action": "synced"})
        await emit({"type": "chapter", "run_id": "run-1", "chapter_number": 2, "action": "skipped"})
        await emit({"type": "completed", "run_id": "run-1", "processed": 2, "synced": 1, "skipped": 1, "total": 2, "remains_paused": True})

    monkeypatch.setattr(autopilot_routes, "get_database", lambda *_args, **_kwargs: database)
    monkeypatch.setattr(autopilot_routes, "get_chapter_aftermath_pipeline", lambda: object())
    monkeypatch.setattr(autopilot_routes, "_request_manual_stop", fake_pause)
    monkeypatch.setattr(autopilot_routes, "resync_all_completed_chapters", fake_service, raising=False)
    monkeypatch.setattr(autopilot_routes, "_resume_autopilot", lambda *args, **kwargs: resume_calls.append(args), raising=False)

    response = await autopilot_routes.resync_all_canonical_aftermath("novel-1")
    assert response.media_type == "text/event-stream"
    frames = _decode_frames([chunk async for chunk in response.body_iterator])

    assert [frame["type"] for frame in frames] == ["started", "chapter", "chapter", "completed"]
    assert pause_calls
    assert not resume_calls


@pytest.mark.asyncio
async def test_full_resync_route_maps_active_marker_to_conflict(monkeypatch):
    database = _Database({
        "id": "novel-1",
        "autopilot_recovery_reason": "canonical_aftermath_full_resync:run-1:9999999999",
    })
    monkeypatch.setattr(autopilot_routes, "get_database", lambda *_args, **_kwargs: database)
    monkeypatch.setattr(autopilot_routes, "get_chapter_aftermath_pipeline", lambda: object())

    with pytest.raises(HTTPException) as exc_info:
        await autopilot_routes.resync_all_canonical_aftermath("novel-1")

    assert exc_info.value.status_code == 409


@pytest.mark.asyncio
async def test_full_resync_route_maps_missing_novel_to_not_found(monkeypatch):
    database = _Database(None)
    monkeypatch.setattr(autopilot_routes, "get_database", lambda *_args, **_kwargs: database)

    with pytest.raises(HTTPException) as exc_info:
        await autopilot_routes.resync_all_canonical_aftermath("missing")

    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_full_resync_route_maps_missing_pipeline_to_unavailable(monkeypatch):
    database = _Database({"id": "novel-1", "autopilot_recovery_reason": ""})
    monkeypatch.setattr(autopilot_routes, "get_database", lambda *_args, **_kwargs: database)
    monkeypatch.setattr(autopilot_routes, "get_chapter_aftermath_pipeline", lambda: None)

    with pytest.raises(HTTPException) as exc_info:
        await autopilot_routes.resync_all_canonical_aftermath("novel-1")

    assert exc_info.value.status_code == 503
