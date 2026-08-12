from contextlib import nullcontext
import sqlite3
from types import SimpleNamespace

import pytest

from application.core.config.config_loader import reload_config
from domain.novel.candidate_chapter import CandidateStatus, GenerationRunState, RunMode
from infrastructure.persistence.database.chapter_candidate_repository import (
    ChapterCandidateRepository,
)
from infrastructure.persistence.database.connection import DatabaseConnection
from interfaces import runtime_state
from interfaces.runtime import AppRuntime, BackendLifecycle, get_backend_lifecycle_settings


def test_app_runtime_shared_novel_state_roundtrip():
    runtime = AppRuntime()
    runtime._shared_state = {}

    runtime.update_shared_novel_state("novel-1", stage="writing", progress=3)

    state = runtime.get_shared_novel_state("novel-1")
    assert state["novel_id"] == "novel-1"
    assert state["stage"] == "writing"
    assert state["progress"] == 3
    assert "_updated_at" in state


def test_app_runtime_missing_state_returns_empty_dict():
    runtime = AppRuntime()
    runtime._shared_state = {}

    assert runtime.get_shared_novel_state("missing") == {}


def test_runtime_state_module_is_canonical_shared_state_accessor(monkeypatch):
    runtime = AppRuntime()
    runtime._shared_state = {}
    monkeypatch.setattr(runtime_state, "_runtime", runtime)

    runtime_state.update_shared_novel_state("novel-2", stage="auditing")

    assert runtime_state.get_shared_novel_state("novel-2")["stage"] == "auditing"


def test_main_reexports_runtime_state_accessors():
    import interfaces.main as main

    assert main.get_shared_novel_state is runtime_state.get_shared_novel_state
    assert main.update_shared_novel_state is runtime_state.update_shared_novel_state
    assert main._get_shared_state is runtime_state._get_shared_state


def test_backend_lifecycle_startup_orchestrates_runtime_steps(monkeypatch):
    calls = []
    lifecycle = BackendLifecycle(
        start_daemon=lambda: calls.append("start_daemon"),
        stop_daemon=lambda: calls.append("stop_daemon"),
        cleanup_orphans=lambda: calls.append("cleanup_orphans"),
    )
    monkeypatch.setattr("interfaces.runtime.os.name", "nt")
    monkeypatch.setattr(
        "infrastructure.persistence.database.write_dispatch.startup_sqlite_writes_bypass_queue",
        lambda: nullcontext(),
    )
    monkeypatch.setattr(lifecycle, "stop_all_running_novels", lambda: calls.append("stop_running"))
    monkeypatch.setattr(lifecycle, "bootstrap_persistence_consumer", lambda: calls.append("persistence"))
    monkeypatch.setattr(lifecycle, "recover_drafts", lambda: calls.append("recover_drafts"))
    monkeypatch.setattr(lifecycle, "init_dag_node_registry", lambda: calls.append("dag_registry"))

    lifecycle.startup(registered_route_count=3)

    assert calls == [
        "cleanup_orphans",
        "stop_running",
        "persistence",
        "recover_drafts",
        "start_daemon",
        "dag_registry",
    ]


def test_backend_lifecycle_skips_orphan_cleanup_when_disabled_by_environment(monkeypatch):
    calls = []
    lifecycle = BackendLifecycle(
        start_daemon=lambda: calls.append("start_daemon"),
        stop_daemon=lambda: calls.append("stop_daemon"),
        cleanup_orphans=lambda: calls.append("cleanup_orphans"),
    )
    monkeypatch.setattr("interfaces.runtime.os.name", "nt")
    monkeypatch.setenv("DISABLE_ORPHAN_CLEANUP", "1")
    monkeypatch.setattr(
        "infrastructure.persistence.database.write_dispatch.startup_sqlite_writes_bypass_queue",
        lambda: nullcontext(),
    )
    monkeypatch.setattr(lifecycle, "stop_all_running_novels", lambda: calls.append("stop_running"))
    monkeypatch.setattr(lifecycle, "bootstrap_persistence_consumer", lambda: calls.append("persistence"))
    monkeypatch.setattr(lifecycle, "recover_drafts", lambda: calls.append("recover_drafts"))
    monkeypatch.setattr(lifecycle, "init_dag_node_registry", lambda: calls.append("dag_registry"))

    lifecycle.startup(registered_route_count=3)

    assert calls == [
        "stop_running",
        "persistence",
        "recover_drafts",
        "start_daemon",
        "dag_registry",
    ]


def test_startup_reset_persists_restart_interruption_reason(tmp_path, monkeypatch):
    """AUTOPILOT-001: restart-stop is distinguishable from a user stop."""
    database_path = tmp_path / "runtime-reset.db"
    connection = sqlite3.connect(database_path)
    connection.execute(
        """
        CREATE TABLE novels (
            id TEXT PRIMARY KEY,
            autopilot_status TEXT NOT NULL,
            autopilot_recovery_reason TEXT NOT NULL DEFAULT '',
            updated_at TEXT
        )
        """
    )
    connection.execute(
        "INSERT INTO novels (id, autopilot_status) VALUES ('novel-1', 'running')"
    )
    connection.commit()

    class _SqliteDatabase:
        def fetch_one(self, sql, params=()):
            row = connection.execute(sql, params).fetchone()
            if row is None:
                return None
            names = [column[0] for column in connection.execute(sql, params).description]
            return dict(zip(names, row))

        def execute(self, sql, params=()):
            return connection.execute(sql, params)

        def commit(self):
            connection.commit()

        def get_connection(self):
            return connection

    database = _SqliteDatabase()
    monkeypatch.setattr("application.paths.get_db_path", lambda: database_path)
    monkeypatch.setattr("infrastructure.persistence.database.connection.get_database", lambda *_args: database)
    lifecycle = BackendLifecycle(start_daemon=lambda: None, stop_daemon=lambda: None)

    lifecycle.stop_all_running_novels()

    row = connection.execute(
        "SELECT autopilot_status, autopilot_recovery_reason FROM novels WHERE id = 'novel-1'"
    ).fetchone()
    connection.close()

    assert row == ("stopped", "service_restart_interrupted")


def test_startup_reset_reconciles_candidate_state_with_real_sqlite(tmp_path, monkeypatch):
    database_path = tmp_path / "candidate-runtime-reset.db"
    database = DatabaseConnection(str(database_path))
    conn = database.get_connection()
    conn.execute(
        "INSERT INTO novels (id, title, slug, autopilot_status, target_chapters) VALUES (?, ?, ?, 'running', ?)",
        ("novel-candidate", "Candidate Novel", "candidate-runtime", 3),
    )
    conn.commit()
    candidates = ChapterCandidateRepository(database)
    candidates.start_run(
        "novel-candidate", run_mode=RunMode.CONTINUOUS, target_chapters=3
    )
    candidate = candidates.create_streaming_candidate(
        novel_id="novel-candidate",
        chapter_number=1,
        title="第一章",
        outline_chain={},
    )
    trace = candidates.start_dag_run(candidate.id, content_revision=0)
    monkeypatch.setattr("application.paths.get_db_path", lambda: database_path)
    monkeypatch.setattr(
        "infrastructure.persistence.database.connection.get_database",
        lambda *_args: database,
    )
    lifecycle = BackendLifecycle(start_daemon=lambda: None, stop_daemon=lambda: None)

    with __import__(
        "infrastructure.persistence.database.write_dispatch", fromlist=["startup_sqlite_writes_bypass_queue"]
    ).startup_sqlite_writes_bypass_queue():
        lifecycle.stop_all_running_novels()

    run = candidates.get_run("novel-candidate")
    assert run.state == GenerationRunState.STOPPED
    assert run.current_candidate_id is None
    assert candidates.get_candidate(candidate.id).status == CandidateStatus.CANCELLED
    assert database.fetch_one(
        "SELECT status FROM candidate_dag_runs WHERE id = ?", (trace["id"],)
    )["status"] == "cancelled"
    database.close_all(skip_checkpoint=True)


def test_backend_lifecycle_shutdown_orchestrates_cleanup(monkeypatch):
    calls = []
    lifecycle = BackendLifecycle(
        start_daemon=lambda: calls.append("start_daemon"),
        stop_daemon=lambda: calls.append("stop_daemon"),
        stop_async_bridge=lambda: calls.append("async_bridge"),
        stop_background_tasks=lambda: calls.append("background_tasks"),
        stop_persistence_consumer=lambda: calls.append("persistence_consumer"),
        stop_managed_resources=lambda: calls.append("managed_resources"),
        start_force_exit_watchdog=lambda: calls.append("watchdog"),
    )
    monkeypatch.setattr(lifecycle, "close_database", lambda skip_checkpoint: calls.append(f"db:{skip_checkpoint}"))
    monkeypatch.setattr(lifecycle, "checkpoint_sqlite_wal_safe", lambda: calls.append("wal"))
    monkeypatch.setattr(lifecycle, "close_llm_service", lambda: calls.append("llm"))
    monkeypatch.setattr(lifecycle, "log_stopped", lambda title: calls.append(title))

    lifecycle.shutdown()

    assert calls == [
        "watchdog",
        "stop_daemon",
        "background_tasks",
        "persistence_consumer",
        "async_bridge",
        "managed_resources",
        "db:True",
        "wal",
        "llm",
        "PlotPilot service stopped",
    ]


def test_shutdown_background_task_service_only_stops_cached_instance(monkeypatch):
    from interfaces.api import dependencies

    calls = []

    class FakeProvider:
        def __init__(self, currsize):
            self.currsize = currsize
            self.service = SimpleNamespace(shutdown=lambda: calls.append("shutdown"))

        def __call__(self):
            calls.append("create_or_fetch")
            return self.service

        def cache_info(self):
            return SimpleNamespace(currsize=self.currsize)

        def cache_clear(self):
            calls.append("cache_clear")

    monkeypatch.setattr(dependencies, "get_background_task_service", FakeProvider(currsize=0))
    dependencies.shutdown_background_task_service_if_initialized()
    assert calls == []

    monkeypatch.setattr(dependencies, "get_background_task_service", FakeProvider(currsize=1))
    dependencies.shutdown_background_task_service_if_initialized()
    assert calls == ["create_or_fetch", "shutdown", "cache_clear"]


def test_shutdown_persistence_queue_only_stops_existing_queue(monkeypatch):
    from application.engine.services import persistence_queue

    calls = []
    monkeypatch.setattr(persistence_queue, "_persistence_queue", None)
    persistence_queue.shutdown_persistence_queue_if_initialized()
    assert calls == []

    fake_queue = SimpleNamespace(stop_consumer=lambda: calls.append("stop_consumer"))
    monkeypatch.setattr(persistence_queue, "_persistence_queue", fake_queue)
    persistence_queue.shutdown_persistence_queue_if_initialized()
    assert calls == ["stop_consumer"]


def test_backend_lifecycle_settings_follow_performance_config(tmp_path):
    config_path = tmp_path / "performance.yaml"
    config_path.write_text(
        """
backend:
  lifecycle:
    startup_reset_max_retries: 5
    startup_reset_retry_backoff_seconds: 0.25
    shutdown_response_delay_seconds: 0.1
    force_exit_timeout_seconds: 3.5
    force_exit_watchdog_poll_seconds: 0.2
""",
        encoding="utf-8",
    )

    try:
        reload_config(str(config_path))
        settings = get_backend_lifecycle_settings()

        assert settings.startup_reset_max_retries == 5
        assert settings.startup_reset_retry_backoff_seconds == pytest.approx(0.25)
        assert settings.shutdown_response_delay_seconds == pytest.approx(0.1)
        assert settings.force_exit_timeout_seconds == pytest.approx(3.5)
        assert settings.force_exit_watchdog_poll_seconds == pytest.approx(0.2)
    finally:
        reload_config()
