import json
import subprocess
import sys
import types
from pathlib import Path

from interfaces.api.settings import BackendSettings
from interfaces.daemon_manager import (
    AutopilotDaemonManager,
    DaemonLifecycleSettings,
    DaemonStatus,
    cleanup_orphan_python_processes,
    is_expected_daemon_shutdown_exception,
    run_autopilot_daemon_process,
)


class FakeEvent:
    def __init__(self):
        self.was_set = False

    def set(self):
        self.was_set = True


class FakeProcess:
    pid = 1234

    def __init__(self, *, alive=True):
        self.alive = alive
        self.join_calls = []
        self.terminated = False
        self.started = False

    def start(self):
        self.started = True

    def is_alive(self):
        return self.alive

    def join(self, timeout=None):
        self.join_calls.append(timeout)

    def terminate(self):
        self.terminated = True
        self.alive = False


def test_expected_daemon_shutdown_exception_detects_chained_interrupt():
    exc = RuntimeError("wrapper")
    exc.__cause__ = KeyboardInterrupt()

    assert is_expected_daemon_shutdown_exception(exc) is True


def test_daemon_manager_status_reads_process_state():
    process = FakeProcess(alive=True)
    manager = AutopilotDaemonManager(
        log_level=20,
        log_file="logs/test.log",
        shared_state_provider=lambda: {},
        lifecycle_settings_provider=lambda: DaemonLifecycleSettings(
            graceful_join_timeout_seconds=0.25,
            terminate_join_timeout_seconds=0.5,
        ),
    )
    manager.process = process

    assert manager.status() == DaemonStatus(running=True, pid=1234)


def test_daemon_manager_start_respects_disable_auto_daemon():
    calls = []
    manager = AutopilotDaemonManager(
        log_level=20,
        log_file="logs/test.log",
        shared_state_provider=lambda: {},
        settings_provider=lambda: BackendSettings(disable_auto_daemon=True),
        process_factory=lambda **kwargs: calls.append(kwargs),
    )

    manager.start()

    assert calls == []
    assert manager.process is None


def test_daemon_manager_binds_the_api_state_publisher_to_the_v1_consumer(
    monkeypatch,
):
    """API routes must reuse the queue that the startup consumer drains."""

    events = []
    queue = type(
        "PersistenceQueue",
        (),
        {"is_consumer_running": lambda self: True, "start_consumer": lambda self: None},
    )()
    process = FakeProcess(alive=True)
    manager = AutopilotDaemonManager(
        log_level=20,
        log_file="logs/test.log",
        shared_state_provider=lambda: {},
        settings_provider=lambda: BackendSettings(disable_auto_daemon=False),
        process_factory=lambda **_kwargs: process,
        event_factory=FakeEvent,
    )

    monkeypatch.setenv("DISABLE_ORPHAN_CLEANUP", "1")
    monkeypatch.setattr(
        "interfaces.daemon_manager.multiprocessing.set_executable", lambda _path: None
    )
    monkeypatch.setattr(
        "interfaces.daemon_manager._write_daemon_pid_registry", lambda *_args, **_kwargs: None
    )
    monkeypatch.setattr(
        "application.engine.services.streaming_bus.init_streaming_bus", lambda: object()
    )
    monkeypatch.setattr(
        "application.engine.services.shared_state_repository.init_shared_state_repository",
        lambda _shared: "shared-repository",
    )
    monkeypatch.setattr(
        "application.engine.services.state_bootstrap.bootstrap_state", lambda: {}
    )
    monkeypatch.setattr(
        "application.engine.services.query_service.init_query_service", lambda _repo: None
    )
    monkeypatch.setattr(
        "application.engine.services.persistence_queue.initialize_persistence_queue",
        lambda: queue,
    )
    monkeypatch.setattr(
        "application.engine.services.persistence_queue.register_persistence_handlers",
        lambda: None,
    )
    monkeypatch.setattr(
        "application.engine.services.persistence_queue.get_persistence_queue", lambda: queue
    )
    monkeypatch.setattr(
        "application.engine.services.state_publisher.init_state_publisher",
        lambda shared, persistence: events.append((shared, persistence)),
    )

    manager.start()

    assert events == [("shared-repository", queue)]


def test_daemon_manager_stop_signals_and_terminates_stuck_process(monkeypatch):
    process = FakeProcess(alive=True)
    event = FakeEvent()
    manager = AutopilotDaemonManager(
        log_level=20,
        log_file="logs/test.log",
        shared_state_provider=lambda: {},
        lifecycle_settings_provider=lambda: DaemonLifecycleSettings(
            graceful_join_timeout_seconds=0.25,
            terminate_join_timeout_seconds=0.5,
        ),
    )
    manager.process = process
    manager.stop_event = event
    monkeypatch.setattr("interfaces.daemon_manager.os.name", "posix")

    manager.stop()

    assert event.was_set is True
    assert process.terminated is True
    assert process.join_calls == [0.25, 0.5]
    assert manager.process is None
    assert manager.stop_event is None


def test_daemon_process_binds_the_injected_queue_before_initializing_state_publisher(
    monkeypatch,
):
    """The child must not construct an unconsumed V2 queue before V1 injection."""

    events = []
    injected_queue = object()
    consumer_queue = object()

    class StopEvent:
        def is_set(self):
            return True

        def wait(self, timeout=None):
            return True

    class FakeDaemon:
        poll_interval = 0

        def _get_active_novels(self):
            return []

    monkeypatch.setattr(
        "interfaces.api.middleware.logging_config.setup_logging",
        lambda **_kwargs: events.append("logging"),
    )
    monkeypatch.setattr(
        "application.engine.services.streaming_bus.inject_stream_queue",
        lambda _queue: events.append("stream"),
    )
    shared_module = __import__(
        "application.engine.services.shared_state_repository", fromlist=["*"]
    )
    monkeypatch.setattr(
        shared_module,
        "inject_shared_dict",
        lambda _shared: events.append("shared"),
    )
    monkeypatch.setattr(shared_module, "get_shared_state_repository", lambda: "repo")
    state_module = __import__(
        "application.engine.services.state_publisher", fromlist=["*"]
    )
    monkeypatch.setattr(
        state_module,
        "init_state_publisher",
        lambda shared, queue: events.append(("publisher", shared, queue)),
    )
    monkeypatch.setattr(
        state_module,
        "get_state_publisher",
        lambda: (_ for _ in ()).throw(AssertionError("default publisher is unsafe")),
    )
    persistence_module = __import__(
        "application.engine.services.persistence_queue", fromlist=["*"]
    )
    monkeypatch.setattr(
        persistence_module,
        "inject_persistence_queue",
        lambda queue: events.append(("persistence", queue)),
    )
    monkeypatch.setattr(
        persistence_module,
        "get_persistence_queue",
        lambda: consumer_queue,
    )
    monkeypatch.setattr(
        "application.engine.services.novel_stop_signal.inject_novel_stop_events",
        lambda: None,
    )
    fake_start_daemon = types.ModuleType("scripts.start_daemon")
    fake_start_daemon.build_daemon = lambda: FakeDaemon()
    monkeypatch.setitem(sys.modules, "scripts.start_daemon", fake_start_daemon)

    run_autopilot_daemon_process(
        StopEvent(),
        20,
        "logs/test.log",
        stream_queue=object(),
        shared_state={},
        persistence_queue=injected_queue,
    )

    assert events.index(("persistence", injected_queue)) < events.index(
        ("publisher", "repo", consumer_queue)
    )


def test_orphan_cleanup_does_not_kill_other_workspace_backend(monkeypatch):
    local_executable = sys.executable
    foreign_executable = str(Path(local_executable).with_name("foreign-python.exe"))
    listed_processes = "\n".join(
        [
            f'101\t"{local_executable}" -m uvicorn interfaces.main:app --port 8015',
            f'202\t"{local_executable}" -m uvicorn interfaces.main:app --port 8015',
            f'303\t"{foreign_executable}" -m uvicorn interfaces.main:app --port 8005',
        ]
    )
    killed_pids = []

    def fake_run(args, **_kwargs):
        if args[0] == "powershell":
            return subprocess.CompletedProcess(args, 0, stdout=listed_processes, stderr="")
        if args[0] == "taskkill":
            killed_pids.append(int(args[-1]))
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        raise AssertionError(f"unexpected subprocess: {args}")

    monkeypatch.setattr("interfaces.daemon_manager.os.getpid", lambda: 101)
    monkeypatch.setattr("interfaces.daemon_manager.get_daemon_lifecycle_settings", DaemonLifecycleSettings)
    monkeypatch.setattr("interfaces.daemon_manager.subprocess.run", fake_run)

    cleanup_orphan_python_processes()

    assert killed_pids == [202]


def test_orphan_cleanup_does_not_kill_current_uvicorn_parent(monkeypatch):
    local_executable = sys.executable
    listed_processes = "\n".join(
        [
            f'101\t0\t"{local_executable}" -m uvicorn interfaces.main:app --port 8015',
            f'107\t101\t"{local_executable}" -m uvicorn interfaces.main:app --port 8015',
            f'202\t0\t"{local_executable}" -m uvicorn interfaces.main:app --port 8015',
        ]
    )
    killed_pids = []

    def fake_run(args, **_kwargs):
        if args[0] == "powershell":
            return subprocess.CompletedProcess(args, 0, stdout=listed_processes, stderr="")
        if args[0] == "taskkill":
            killed_pids.append(int(args[-1]))
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        raise AssertionError(f"unexpected subprocess: {args}")

    monkeypatch.setattr("interfaces.daemon_manager.os.getpid", lambda: 107)
    monkeypatch.setattr("interfaces.daemon_manager.get_daemon_lifecycle_settings", DaemonLifecycleSettings)
    monkeypatch.setattr("interfaces.daemon_manager.subprocess.run", fake_run)

    cleanup_orphan_python_processes()

    assert killed_pids == [202]


def test_orphan_cleanup_wmic_fallback_does_not_kill_current_parent(monkeypatch):
    local_executable = sys.executable
    wmic_processes = "\n".join(
        [
            f'"{local_executable}" -m uvicorn interfaces.main:app --port 8015 101',
            f'"{local_executable}" -m uvicorn interfaces.main:app --port 8015 107',
            f'"{local_executable}" -m uvicorn interfaces.main:app --port 8015 202',
        ]
    )
    killed_pids = []

    def fake_run(args, **_kwargs):
        if args[0] == "powershell":
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if args[0] == "wmic":
            return subprocess.CompletedProcess(args, 0, stdout=wmic_processes, stderr="")
        if args[0] == "taskkill":
            killed_pids.append(int(args[-1]))
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        raise AssertionError(f"unexpected subprocess: {args}")

    monkeypatch.setattr("interfaces.daemon_manager.os.getpid", lambda: 107)
    monkeypatch.setattr("interfaces.daemon_manager.os.getppid", lambda: 101)
    monkeypatch.setattr("interfaces.daemon_manager.get_daemon_lifecycle_settings", DaemonLifecycleSettings)
    monkeypatch.setattr("interfaces.daemon_manager.subprocess.run", fake_run)

    cleanup_orphan_python_processes()

    assert killed_pids == [202]


def test_daemon_manager_stop_respects_disabled_orphan_cleanup(monkeypatch):
    cleanup_calls = []
    manager = AutopilotDaemonManager(
        log_level=20,
        log_file="logs/test.log",
        shared_state_provider=lambda: {},
    )
    monkeypatch.setattr("interfaces.daemon_manager.os.name", "nt")
    monkeypatch.setenv("DISABLE_ORPHAN_CLEANUP", "1")
    monkeypatch.setattr(manager, "cleanup_orphans", lambda: cleanup_calls.append(True))

    manager.stop()

    assert cleanup_calls == []


def test_orphan_cleanup_terminates_only_a_registered_stale_daemon(monkeypatch, tmp_path):
    """DAEMON-001: a Python spawn child survives a killed launcher only when its local PID record is ignored."""
    registry_path = tmp_path / "autopilot-daemon.json"
    registry_path.write_text(
        json.dumps({"pid": 202, "parent_pid": 999, "workspace_root": "W:/novel/test"}),
        encoding="utf-8",
    )
    local_executable = sys.executable
    listed_processes = "\n".join(
        [
            f'101\t0\t"{local_executable}" -m uvicorn interfaces.main:app --port 8005',
            '202\t999\t"C:\\Python\\pythonw.exe" -c "from multiprocessing.spawn import spawn_main; spawn_main(parent_pid=999, pipe_handle=1)" --multiprocessing-fork',
            '303\t888\t"C:\\Python\\pythonw.exe" -c "from multiprocessing.spawn import spawn_main; spawn_main(parent_pid=888, pipe_handle=2)" --multiprocessing-fork',
        ]
    )
    killed_pids = []

    def fake_run(args, **_kwargs):
        if args[0] == "powershell":
            return subprocess.CompletedProcess(args, 0, stdout=listed_processes, stderr="")
        if args[0] == "taskkill":
            killed_pids.append(int(args[-1]))
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        raise AssertionError(f"unexpected subprocess: {args}")

    monkeypatch.setattr("interfaces.daemon_manager.os.getpid", lambda: 101)
    monkeypatch.setattr("interfaces.daemon_manager.os.getppid", lambda: 0)
    monkeypatch.setattr("interfaces.daemon_manager.get_daemon_lifecycle_settings", DaemonLifecycleSettings)
    monkeypatch.setattr("interfaces.daemon_manager._daemon_pid_registry_path", lambda: registry_path)
    monkeypatch.setattr("interfaces.daemon_manager.subprocess.run", fake_run)

    cleanup_orphan_python_processes()

    assert killed_pids == [202]
    assert registry_path.exists() is False


def test_daemon_manager_records_spawned_child_for_future_launcher_cleanup(monkeypatch, tmp_path):
    """DAEMON-002: every Windows daemon launch records the exact child PID before the launcher can disappear."""
    registry_path = tmp_path / "autopilot-daemon.json"
    process = FakeProcess(alive=True)
    event = FakeEvent()
    queue = type(
        "PersistenceQueue",
        (),
        {"is_consumer_running": lambda self: True, "start_consumer": lambda self: None},
    )()
    manager = AutopilotDaemonManager(
        log_level=20,
        log_file="logs/test.log",
        shared_state_provider=lambda: {},
        settings_provider=lambda: BackendSettings(disable_auto_daemon=False),
        process_factory=lambda **_kwargs: process,
        event_factory=lambda: event,
    )
    cleanup_calls = []
    executable_calls = []

    monkeypatch.setattr("interfaces.daemon_manager.os.name", "nt")
    monkeypatch.setattr("interfaces.daemon_manager._daemon_pid_registry_path", lambda: registry_path)
    monkeypatch.setattr("interfaces.daemon_manager.multiprocessing.set_executable", executable_calls.append)
    monkeypatch.setattr(manager, "cleanup_orphans", lambda: cleanup_calls.append(True))
    monkeypatch.setattr(
        "application.engine.services.streaming_bus.init_streaming_bus",
        lambda: object(),
    )
    monkeypatch.setattr(
        "application.engine.services.shared_state_repository.init_shared_state_repository",
        lambda _shared: object(),
    )
    monkeypatch.setattr(
        "application.engine.services.state_bootstrap.bootstrap_state",
        lambda: {},
    )
    monkeypatch.setattr(
        "application.engine.services.query_service.init_query_service",
        lambda _repo: None,
    )
    monkeypatch.setattr(
        "application.engine.services.persistence_queue.initialize_persistence_queue",
        lambda: queue,
    )
    monkeypatch.setattr(
        "application.engine.services.persistence_queue.register_persistence_handlers",
        lambda: None,
    )
    monkeypatch.setattr(
        "application.engine.services.persistence_queue.get_persistence_queue",
        lambda: queue,
    )

    manager.start()

    assert process.started is True
    assert cleanup_calls == [True]
    assert executable_calls == [sys.executable]
    record = json.loads(registry_path.read_text(encoding="utf-8"))
    assert record["pid"] == 1234
    assert record["parent_pid"] > 0
