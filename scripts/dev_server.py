"""Developer launcher helpers for the PlotPilot API and Vite workbench."""

from __future__ import annotations

import socket
import subprocess
import sys
import time
import webbrowser
from argparse import ArgumentParser
from collections.abc import Callable
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import urlopen


BACKEND_PORT = 8005
FRONTEND_PORT = 3000
BACKEND_HEALTH_URL = f"http://127.0.0.1:{BACKEND_PORT}/docs"
FRONTEND_HEALTH_URL = f"http://127.0.0.1:{FRONTEND_PORT}/"


def wait_for_http_ready(
    url: str,
    *,
    timeout_seconds: float = 60,
    poll_interval: float = 0.25,
) -> None:
    """Return after *url* responds successfully, or raise on timeout."""

    deadline = time.monotonic() + timeout_seconds
    last_failure: Exception | None = None

    while time.monotonic() < deadline:
        try:
            with urlopen(url, timeout=max(1.0, poll_interval)) as response:
                if 200 <= response.status < 400:
                    return
        except (HTTPError, URLError, OSError, TimeoutError) as exc:
            last_failure = exc

        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(poll_interval, remaining))

    suffix = f" Last failure: {last_failure}" if last_failure else ""
    raise TimeoutError(f"{url} did not become ready within {timeout_seconds} seconds.{suffix}")


def _is_http_ready(url: str) -> bool:
    try:
        wait_for_http_ready(url, timeout_seconds=0.2, poll_interval=0.05)
    except TimeoutError:
        return False
    return True


def _port_is_in_use(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=0.2):
            return True
    except OSError:
        return False


def _default_python_executable(project_root: Path) -> str:
    virtual_environment_python = project_root / ".venv" / "Scripts" / "python.exe"
    if virtual_environment_python.exists():
        return str(virtual_environment_python)
    return sys.executable


def _start_process(
    command: list[str],
    *,
    cwd: Path,
    popen: Callable[..., object],
) -> object:
    creationflags = (
        getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        | getattr(subprocess, "DETACHED_PROCESS", 0)
    )
    return popen(
        command,
        cwd=str(cwd),
        stdout=subprocess.DEVNULL,
        stderr=subprocess.STDOUT,
        creationflags=creationflags,
    )


def start_development_servers(
    project_root: Path,
    *,
    python_executable: str | None = None,
    npm_command: str = "npm.cmd",
    is_ready: Callable[[str], bool] = _is_http_ready,
    port_in_use: Callable[[int], bool] = _port_is_in_use,
    popen: Callable[..., object] = subprocess.Popen,
    wait_for_ready: Callable[..., None] = wait_for_http_ready,
    browser_opener: Callable[[str], object] = webbrowser.open,
) -> None:
    """Start the API and Vite workbench in dependency order."""

    project_root = Path(project_root).resolve()
    python_executable = python_executable or _default_python_executable(project_root)

    if not is_ready(BACKEND_HEALTH_URL):
        if not port_in_use(BACKEND_PORT):
            _start_process(
                [
                    python_executable,
                    "-m",
                    "uvicorn",
                    "interfaces.main:app",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(BACKEND_PORT),
                ],
                cwd=project_root,
                popen=popen,
            )
        wait_for_ready(BACKEND_HEALTH_URL, timeout_seconds=60, poll_interval=0.25)

    if not is_ready(FRONTEND_HEALTH_URL):
        if not port_in_use(FRONTEND_PORT):
            _start_process(
                [
                    npm_command,
                    "run",
                    "dev",
                    "--",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(FRONTEND_PORT),
                ],
                cwd=project_root / "frontend",
                popen=popen,
            )
        wait_for_ready(FRONTEND_HEALTH_URL, timeout_seconds=60, poll_interval=0.25)

    browser_opener(FRONTEND_HEALTH_URL)


def _listening_pids(port: int) -> list[int]:
    """Return PIDs listening on a specific local TCP port on Windows."""

    result = subprocess.run(
        ["netstat", "-ano", "-p", "tcp"],
        capture_output=True,
        text=True,
        check=False,
    )
    pids: list[int] = []
    for line in result.stdout.splitlines():
        columns = line.split()
        if len(columns) < 5 or columns[3].upper() != "LISTENING":
            continue
        try:
            local_port = int(columns[1].rsplit(":", maxsplit=1)[-1])
            process_id = int(columns[4])
        except ValueError:
            continue
        if local_port == port:
            pids.append(process_id)
    return pids


def _terminate_process_tree(process_id: int) -> None:
    subprocess.run(
        ["taskkill.exe", "/PID", str(process_id), "/T", "/F"],
        check=False,
        capture_output=True,
        text=True,
    )


def stop_development_servers(
    *,
    listening_pids: Callable[[int], list[int]] = _listening_pids,
    terminate_process_tree: Callable[[int], object] = _terminate_process_tree,
) -> list[int]:
    """Stop the unique process trees currently listening on PlotPilot dev ports."""

    process_ids = sorted({process_id for port in (BACKEND_PORT, FRONTEND_PORT) for process_id in listening_pids(port)})
    for process_id in process_ids:
        terminate_process_tree(process_id)
    return process_ids


def _project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def main() -> int:
    parser = ArgumentParser(description="Start or stop the PlotPilot development servers.")
    parser.add_argument("command", choices=("start", "stop"))
    arguments = parser.parse_args()

    if arguments.command == "start":
        start_development_servers(_project_root())
        print(f"PlotPilot development workbench is ready at {FRONTEND_HEALTH_URL}")
        return 0

    stopped_processes = stop_development_servers()
    if stopped_processes:
        print("Stopped PlotPilot development process trees: " + ", ".join(map(str, stopped_processes)))
    else:
        print("No PlotPilot development process is listening on ports 8005 or 3000.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
