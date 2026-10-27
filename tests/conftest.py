"""What the tests share: a project in a fresh git repository under a temporary home with a bridge
configured (GitHub's, or Slack's), the fake of its API, a clock the test moves, and a bridge wired to
all three."""

import subprocess
import threading

import fake_slack
import pytest
from fake_github import TOKEN, FakeGitHub
from orchestra.config import load_project
from orchestra.events import Events
from orchestra.metrics import ProjectStore, for_project

from orchestra_bridges import config as cfg
from orchestra_bridges.github.api import GitHub
from orchestra_bridges.github.bridge import MACHINE, Bridge
from orchestra_bridges.slack import bridge as slack_bridge
from orchestra_bridges.slack.api import Slack

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


SLACK_TOML = """[bridges]
people = "people.toml"

[bridges.slack]
every = "60s"
lookback = "1h"
max_catchup = "6h"
overlap = "10m"
port = 0
api = "{api}"
{extra}
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


@pytest.fixture
def project(tmp_path, monkeypatch, github):
    return new_project(tmp_path, monkeypatch, TOML.format(api=github.base))


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def slack(clock):
    with fake_slack.FakeSlack(clock) as fake:
        yield fake


@pytest.fixture
def slack_toml():
    """Extra lines for ``[bridges.slack]``; a test overrides it to change a setting."""
    return ""


@pytest.fixture
def slack_project(tmp_path, monkeypatch, slack, slack_toml):
    return new_project(tmp_path, monkeypatch, SLACK_TOML.format(api=slack.base, extra=slack_toml))


@pytest.fixture
def make_slack(slack_project, slack, clock):
    """A new Slack bridge over the project's bus and store, not yet started (call it again for a
    restart). Its Retry-After waits are recorded, not slept."""
    def make():
        conf = cfg.slack(slack_project.config.raw, slack_project.repo_root)
        events = Events.for_project(slack_project, source=slack_bridge.MACHINE)
        store = ProjectStore(for_project(slack_project), slack_project.key)
        api = Slack(fake_slack.TOKEN, slack.base, sleep=lambda s: None)
        return slack_bridge.Bridge(conf, api, events, store, clock=clock)
    return make


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
