"""What the tests share: a project in a fresh git repository under a temporary home, and the
environment for a program under test (this checkout first on ``PYTHONPATH``)."""

import os
import subprocess
from pathlib import Path

from orchestra.config import load_project

ROOT = Path(__file__).resolve().parents[1]                 # the repository these tests run from


def checkout_env(**extra: str) -> dict[str, str]:
    """The environment for an ``orchestra run`` or a scaffolded program under test: this checkout
    first on ``PYTHONPATH``, so what it starts runs the code under test, not whatever the
    environment's editable install points at (another checkout, or none for an export of a commit)."""
    path = os.pathsep.join(p for p in (str(ROOT), os.environ.get("PYTHONPATH")) if p)
    return {**os.environ, **extra, "PYTHONPATH": path}


def new_project(tmp_path, monkeypatch, toml: str):
    monkeypatch.setenv("ORCHESTRATOR_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("ORCHESTRA_EVENTS_URL", raising=False)
    monkeypatch.delenv("ORCHESTRA_METRICS_URL", raising=False)
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    (repo / "orchestra.toml").write_text(toml)
    monkeypatch.setenv("ORCHESTRA_REPO", str(repo))
    return load_project(repo)
