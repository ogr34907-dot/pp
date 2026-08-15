"""Regression coverage for the database-backed DAG daemon adapter."""

import pytest

from application.engine.dag import daemon_runner
from application.engine.dag.models import get_default_dag


def test_daemon_runner_keeps_legacy_data_root_out_of_database_version_manager(monkeypatch):
    """The database manager has no filesystem data-root argument."""

    class VersionManager:
        pass

    monkeypatch.setattr(daemon_runner, "DAGVersionManager", VersionManager)

    runner = daemon_runner.DAGDaemonRunner(data_root="legacy-filesystem-root")

    assert isinstance(runner._version_mgr, VersionManager)


@pytest.mark.asyncio
async def test_daemon_initial_state_carries_saved_node_timeout_and_retry_settings(monkeypatch):
    dag = get_default_dag()
    node = dag.get_node("exec_writer")
    node.config.timeout_seconds = 120
    node.config.max_retries = 3

    class VersionManager:
        def load_latest(self, novel_id):
            assert novel_id == "novel-1"
            return dag

    runner = daemon_runner.DAGDaemonRunner()
    runner._version_mgr = VersionManager()

    captured = {}

    async def run(saved_dag, initial_state, thread_id):
        captured["dag"] = saved_dag
        captured["initial_state"] = initial_state
        captured["thread_id"] = thread_id
        return type("Result", (), {"status": "completed", "total_duration_ms": 0})()

    monkeypatch.setattr(runner._engine, "run", run)

    await runner.run_novel("novel-1")

    assert captured["dag"] is dag
    assert captured["thread_id"] == "novel_novel-1"
    config = captured["initial_state"]["node_configs"]["exec_writer"]
    assert config["timeout_seconds"] == 120
    assert config["max_retries"] == 3
