from pathlib import Path

from scripts.setup import check_environment


def test_project_python_prefers_repository_virtualenv(tmp_path, monkeypatch):
    venv_python = tmp_path / ".venv" / "Scripts" / "python.exe"
    venv_python.parent.mkdir(parents=True)
    venv_python.write_text("", encoding="ascii")

    monkeypatch.setattr(check_environment, "PROJECT_ROOT", tmp_path)

    assert check_environment.project_python() == venv_python


def test_pip_install_command_uses_project_python(tmp_path, monkeypatch):
    venv_python = tmp_path / ".venv" / "Scripts" / "python.exe"
    venv_python.parent.mkdir(parents=True)
    venv_python.write_text("", encoding="ascii")

    monkeypatch.setattr(check_environment, "PROJECT_ROOT", tmp_path)

    assert check_environment.pip_install_command("fastapi") == [
        str(venv_python),
        "-m",
        "pip",
        "install",
        "fastapi",
    ]
