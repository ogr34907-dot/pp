import contextlib
import json

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from interfaces.api.v1.engine import autopilot_routes
from infrastructure.persistence.database.connection import DatabaseConnection


class _Database:
    def __init__(self, row):
        self.row = row
        self.updates = []

    def fetch_one(self, query, params=()):
        if "FROM novels" in query:
            return self.row
        return None

    def execute(self, query, params=()):
        self.updates.append((query, params))

    def commit(self):
        return None


class _StateDatabase(_Database):
    def __init__(self, row):
        super().__init__(row)
        self.updates = []

    def execute(self, query, params=()):
        self.updates.append((query, params))

    def commit(self):
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
    resume_calls = []

    async def fake_service(*, novel_id, database, aftermath_pipeline, emit):
        await emit({"type": "started", "run_id": "run-1", "total": 2, "pending_chapters": 2})
        await emit({"type": "chapter", "run_id": "run-1", "chapter_number": 1, "action": "synced"})
        await emit({"type": "chapter", "run_id": "run-1", "chapter_number": 2, "action": "skipped"})
        await emit({"type": "completed", "run_id": "run-1", "processed": 2, "synced": 1, "skipped": 1, "total": 2, "remains_paused": True})

    monkeypatch.setattr(autopilot_routes, "get_database", lambda *_args, **_kwargs: database)
    monkeypatch.setattr(autopilot_routes, "get_chapter_aftermath_pipeline", lambda: object())
    monkeypatch.setattr(autopilot_routes, "resync_all_completed_chapters", fake_service, raising=False)
    monkeypatch.setattr(autopilot_routes, "_resume_autopilot", lambda *args, **kwargs: resume_calls.append(args), raising=False)

    response = await autopilot_routes.resync_all_canonical_aftermath("novel-1")
    assert response.media_type == "text/event-stream"
    frames = _decode_frames([chunk async for chunk in response.body_iterator])

    assert [frame["type"] for frame in frames] == ["started", "chapter", "chapter", "completed"]
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


@pytest.mark.asyncio
async def test_full_resync_persists_paused_stage_before_service_claim(monkeypatch):
    database = _StateDatabase({"id": "novel-1", "autopilot_recovery_reason": ""})
    observed = []

    async def fake_service(*, database, **kwargs):
        observed.append(any("current_stage = 'paused_for_review'" in query for query, _ in database.updates))
        await kwargs["emit"]({"type": "completed", "run_id": "run-1", "total": 0, "processed": 0})

    monkeypatch.setattr(autopilot_routes, "get_database", lambda *_args, **_kwargs: database)
    monkeypatch.setattr(autopilot_routes, "get_chapter_aftermath_pipeline", lambda: object())
    monkeypatch.setattr(autopilot_routes, "resync_all_completed_chapters", fake_service)

    response = await autopilot_routes.resync_all_canonical_aftermath("novel-1")
    [chunk async for chunk in response.body_iterator]

    assert observed == [True]


@pytest.mark.asyncio
async def test_resume_and_start_reject_active_full_resync_without_resume_signal(monkeypatch):
    database = _Database({
        "id": "novel-1",
        "autopilot_recovery_reason": "canonical_aftermath_full_resync:run-1:9999999999",
    })
    signals = []
    monkeypatch.setattr(autopilot_routes, "get_database", lambda *_args, **_kwargs: database)
    monkeypatch.setattr(autopilot_routes, "_get_shared_state_for_novel", lambda *_args: None)
    monkeypatch.setattr(autopilot_routes, "get_novel_repository", lambda: None)
    monkeypatch.setattr(autopilot_routes, "publish_start_signal", lambda novel_id: signals.append(novel_id), raising=False)

    with pytest.raises(HTTPException) as resume_exc:
        await autopilot_routes.resume_from_review("novel-1")
    with pytest.raises(HTTPException) as start_exc:
        await autopilot_routes.start_autopilot("novel-1")

    assert resume_exc.value.status_code == 409
    assert start_exc.value.status_code == 409
    assert signals == []


def test_full_resync_asgi_route_has_exact_path_and_sse_content_type(monkeypatch):
    database = _Database({"id": "novel-1", "autopilot_recovery_reason": ""})

    async def fake_service(*, emit, **kwargs):
        await emit({"type": "started", "run_id": "run-asgi", "total": 0, "pending_chapters": 0})
        await emit({"type": "completed", "run_id": "run-asgi", "processed": 0, "synced": 0, "skipped": 0, "total": 0, "remains_paused": True})

    monkeypatch.setattr(autopilot_routes, "get_database", lambda *_args, **_kwargs: database)
    monkeypatch.setattr(autopilot_routes, "get_chapter_aftermath_pipeline", lambda: object())
    monkeypatch.setattr(autopilot_routes, "resync_all_completed_chapters", fake_service)

    app = FastAPI()
    app.include_router(autopilot_routes.router, prefix="/api/v1")
    with TestClient(app) as client:
        response = client.post("/api/v1/autopilot/novel-1/canonical-aftermath/resync-all")

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert '"type": "started"' in response.text
    assert '"type": "completed"' in response.text


@pytest.mark.asyncio
async def test_full_resync_persists_pause_state_in_real_database(tmp_path, monkeypatch):
    database = DatabaseConnection(str(tmp_path / "pause-state.db"))
    database.execute("INSERT INTO novels (id, title, slug) VALUES ('novel-1', 'Demo', 'demo')")
    database.commit()

    async def fake_service(*, emit, **kwargs):
        await emit({"type": "completed", "run_id": "run-db", "total": 0, "processed": 0})

    monkeypatch.setattr(autopilot_routes, "get_database", lambda *_args, **_kwargs: database)
    monkeypatch.setattr(autopilot_routes, "get_chapter_aftermath_pipeline", lambda: object())
    monkeypatch.setattr(autopilot_routes, "resync_all_completed_chapters", fake_service)

    response = await autopilot_routes.resync_all_canonical_aftermath("novel-1")
    [chunk async for chunk in response.body_iterator]

    row = database.fetch_one(
        "SELECT autopilot_status, current_stage, autopilot_recovery_reason FROM novels WHERE id = ?",
        ("novel-1",),
    )
    assert row["autopilot_status"] == "stopped"
    assert row["current_stage"] == "paused_for_review"
    assert row["autopilot_recovery_reason"] == "paused_for_review_preserved"


def test_pause_sync_works_without_direct_sqlite_write_environment(tmp_path, monkeypatch):
    database = DatabaseConnection(str(tmp_path / "pause-env.db"))
    database.execute("INSERT INTO novels (id, title, slug) VALUES ('novel-1', 'Demo', 'demo')")
    database.commit()
    monkeypatch.delenv("PLOTPILOT_ALLOW_DIRECT_SQLITE_WRITES", raising=False)
    monkeypatch.setattr(autopilot_routes, "get_database", lambda *_args, **_kwargs: database)

    assert autopilot_routes._persist_full_resync_pause_sync("novel-1") == "ok"


def test_pause_sync_cas_preserves_marker_written_by_another_connection(tmp_path, monkeypatch):
    db_path = str(tmp_path / "pause-race.db")
    database = DatabaseConnection(db_path)
    other = DatabaseConnection(db_path)
    database.execute("INSERT INTO novels (id, title, slug) VALUES ('novel-1', 'Demo', 'demo')")
    database.commit()

    class _RacingDatabase:
        def fetch_one(self, query, params=()):
            return database.fetch_one(query, params)

        @contextlib.contextmanager
        def transaction(self):
            other.execute(
                "UPDATE novels SET autopilot_recovery_reason = ? WHERE id = ?",
                ("canonical_aftermath_full_resync:other-run:9999999999", "novel-1"),
            )
            other.commit()
            with database.transaction() as conn:
                yield conn

    monkeypatch.setattr(autopilot_routes, "get_database", lambda *_args, **_kwargs: _RacingDatabase())

    assert autopilot_routes._persist_full_resync_pause_sync("novel-1") == "conflict"
    row = other.fetch_one("SELECT autopilot_recovery_reason FROM novels WHERE id = ?", ("novel-1",))
    assert row["autopilot_recovery_reason"].startswith("canonical_aftermath_full_resync:other-run:")


@pytest.mark.asyncio
async def test_pause_race_preserves_new_active_marker_and_does_not_start_worker(monkeypatch):
    database = _Database({"id": "novel-1", "autopilot_recovery_reason": ""})
    started = []

    async def fake_service(**kwargs):
        started.append(True)

    def fake_pause_sync(_novel_id):
        return "conflict"

    monkeypatch.setattr(autopilot_routes, "get_database", lambda *_args, **_kwargs: database)
    monkeypatch.setattr(autopilot_routes, "get_chapter_aftermath_pipeline", lambda: object())
    monkeypatch.setattr(autopilot_routes, "_persist_full_resync_pause_sync", fake_pause_sync)
    monkeypatch.setattr(autopilot_routes, "resync_all_completed_chapters", fake_service)

    with pytest.raises(HTTPException) as exc_info:
        await autopilot_routes.resync_all_canonical_aftermath("novel-1")

    assert exc_info.value.status_code == 409
    assert started == []
