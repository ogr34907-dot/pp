from __future__ import annotations

import sys
import tomllib
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_release_and_ci_require_the_python_314_series():
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    workflow = (ROOT / ".github" / "workflows" / "backend-ci.yml").read_text(
        encoding="utf-8"
    )

    assert metadata["project"]["requires-python"] == ">=3.14,<3.15"
    assert 'python-version: "3.14"' in workflow
    assert sys.version_info[:2] == (3, 14)
