from __future__ import annotations

import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from scripts.dev_server import (
    start_development_servers,
    start_frontend_development_server,
    stop_development_servers,
    stop_frontend_development_server,
    wait_for_http_ready,
)


class _ReadyHandler(BaseHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - required stdlib handler name
        self.send_response(204)
        self.end_headers()

    def log_message(self, _format: str, *_args: object) -> None:
        return


def _unused_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def test_wait_for_http_ready_waits_for_a_real_http_response() -> None:
    port = _unused_loopback_port()
    server_started = threading.Event()

    def serve_after_delay() -> None:
        time.sleep(0.2)
        server = HTTPServer(("127.0.0.1", port), _ReadyHandler)
        server_started.set()
        try:
            server.handle_request()
        finally:
            server.server_close()

    thread = threading.Thread(target=serve_after_delay, daemon=True)
    thread.start()

    started_at = time.monotonic()
    wait_for_http_ready(f"http://127.0.0.1:{port}/ready", timeout_seconds=2, poll_interval=0.02)
    elapsed = time.monotonic() - started_at

    thread.join(timeout=1)
    assert server_started.is_set()
    assert elapsed >= 0.15
    assert not thread.is_alive()


def test_wait_for_http_ready_times_out_when_no_server_is_available() -> None:
    with pytest.raises(TimeoutError, match="did not become ready"):
        wait_for_http_ready(
            f"http://127.0.0.1:{_unused_loopback_port()}/ready",
            timeout_seconds=0.05,
            poll_interval=0.01,
        )


def test_start_development_servers_starts_only_the_hidden_local_workbench(tmp_path) -> None:
    (tmp_path / "frontend").mkdir()
    python_executable = tmp_path / "python.exe"
    python_windowless_executable = tmp_path / "pythonw.exe"
    python_executable.touch()
    python_windowless_executable.touch()
    calls: list[tuple[str, str]] = []
    popen_arguments: list[dict[str, object]] = []

    def fake_popen(command, **kwargs):
        calls.append(("spawn", " ".join(map(str, command))))
        popen_arguments.append(kwargs)
        return object()

    def fake_wait(url: str, **_kwargs) -> None:
        calls.append(("wait", url))

    def fake_browser(url: str) -> None:
        calls.append(("browser", url))

    start_development_servers(
        tmp_path,
        python_executable=str(python_executable),
        npm_command="npm.cmd",
        is_ready=lambda _url: False,
        port_in_use=lambda _port: False,
        popen=fake_popen,
        wait_for_ready=fake_wait,
        browser_opener=fake_browser,
    )

    assert calls == [
        (
            "spawn",
            f"{python_windowless_executable} -m uvicorn interfaces.main:app --host 127.0.0.1 --port 8005",
        ),
        ("wait", "http://127.0.0.1:8005/"),
        ("browser", "http://127.0.0.1:8005/"),
    ]
    assert popen_arguments[0]["creationflags"] & subprocess.CREATE_NO_WINDOW


def test_stop_development_servers_stops_only_the_local_workbench_listener() -> None:
    queried_ports: list[int] = []
    stopped: list[int] = []

    def listening_pids(port: int) -> list[int]:
        queried_ports.append(port)
        return {8005: [101, 202], 3000: [202, 303]}[port]

    stopped_processes = stop_development_servers(
        listening_pids=listening_pids,
        terminate_process_tree=stopped.append,
    )

    assert queried_ports == [8005]
    assert stopped_processes == [101, 202]
    assert stopped == [101, 202]


def test_start_frontend_development_server_is_an_explicit_vite_only_entrypoint(tmp_path) -> None:
    (tmp_path / "frontend").mkdir()
    calls: list[tuple[str, str]] = []

    def fake_popen(command, **_kwargs):
        calls.append(("spawn", " ".join(map(str, command))))
        return object()

    def fake_wait(url: str, **_kwargs) -> None:
        calls.append(("wait", url))

    def fake_browser(url: str) -> None:
        calls.append(("browser", url))

    start_frontend_development_server(
        tmp_path,
        npm_command="npm.cmd",
        is_ready=lambda _url: False,
        port_in_use=lambda _port: False,
        popen=fake_popen,
        wait_for_ready=fake_wait,
        browser_opener=fake_browser,
    )

    assert calls == [
        ("spawn", "npm.cmd run dev -- --host 127.0.0.1 --port 3000"),
        ("wait", "http://127.0.0.1:3000/"),
        ("browser", "http://127.0.0.1:3000/"),
    ]


def test_stop_frontend_development_server_only_queries_vite_port() -> None:
    queried_ports: list[int] = []
    stopped: list[int] = []

    def listening_pids(port: int) -> list[int]:
        queried_ports.append(port)
        return [303] if port == 3000 else [101]

    stopped_processes = stop_frontend_development_server(
        listening_pids=listening_pids,
        terminate_process_tree=stopped.append,
    )

    assert queried_ports == [3000]
    assert stopped_processes == [303]
    assert stopped == [303]
