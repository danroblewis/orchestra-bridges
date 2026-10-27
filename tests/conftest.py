"""What the tests share: a project in a fresh git repository under a temporary home with the GitHub
bridge configured, the fake GitHub, a clock the test moves, and a bridge wired to all three."""

import subprocess
import threading

import pytest
from fake_github import TOKEN, FakeGitHub
from orchestra.config import load_project
from orchestra.events import Events
from orchestra.metrics import ProjectStore, for_project

from orchestra_bridges import config as cfg
from orchestra_bridges.github.api import GitHub
from orchestra_bridges.github.bridge import MACHINE, Bridge

T0 = 1_793_000_000.0         # 2026-10-26, a fixed "now" the fake's world is built around

TOML = """[bridges]
people = "people.toml"

[bridges.github]
repos = ["microsoft/vscode"]
every = "60s"
lookback = "1h"
max_catchup = "6h"
overlap = "10m"
api = "{api}"
"""


class Clock:
    def __init__(self, t: float = T0):
        self.t = t

    def __call__(self) -> float:
        return self.t


@pytest.fixture
def github():
    with FakeGitHub() as fake:
        yield fake


@pytest.fixture
def project(tmp_path, monkeypatch, github):
    monkeypatch.setenv("ORCHESTRATOR_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("ORCHESTRA_EVENTS_URL", raising=False)
    monkeypatch.delenv("ORCHESTRA_METRICS_URL", raising=False)
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    (repo / "orchestra.toml").write_text(TOML.format(api=github.base))
    monkeypatch.setenv("ORCHESTRA_REPO", str(repo))
    return load_project(repo)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def make_bridge(project, github, clock):
    """A new bridge over the project's bus and store, not yet resumed (call it again for a restart)."""
    def make():
        conf = cfg.github(project.config.raw, project.repo_root)
        events = Events.for_project(project, source=MACHINE)
        store = ProjectStore(for_project(project), project.key)
        b = Bridge(conf, GitHub(TOKEN, github.base, sleep=lambda s: None, clock=clock), events, store, clock=clock)
        return b
    return make


def bus(project) -> list[dict]:
    return Events.for_project(project).after(0)[0]


def published(project, type_=None) -> list[dict]:
    return [e for e in bus(project) if e["type"] != "orchestra.machine.started" and (type_ is None or e["type"] == type_)]


def run_once(bridge) -> None:
    stop = threading.Event()
    bridge.run(stop, once=True)
