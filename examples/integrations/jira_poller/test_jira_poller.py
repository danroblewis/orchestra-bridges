"""jira_poller against the kit's Jira fake: two polls (a restart between them) relay each status
change once, as who made it; ``measure`` prints the issues by status; no text or token leaves.

Run: ``uv run python -m pytest examples/integrations/jira_poller/test_jira_poller.py`` (in an environment with orchestra-bridges)."""

import importlib.util
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from orchestra_bridges.kits.fakes import CANARY, FakeJira
from orchestra_bridges.kits.fakes.base import load_fixture
from orchestra_bridges.kits.fakes.jira import stamp

HERE = Path(__file__).resolve().parent
TOKEN = "test-token-not-real"


def program():
    spec = importlib.util.spec_from_file_location("jira_poller", HERE / "jira_poller.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def recent_world() -> dict:
    """The kit's fixture, its times moved to the last hours (the poller reads back a day)."""
    world, now = load_fixture(None, "jira.json"), datetime.now(UTC)
    for n, issue in enumerate(world["issues"]):
        t = stamp(now - timedelta(hours=3 - n))
        issue["updated"] = t
        for h in issue.get("history") or []:
            h["at"] = t
    return world


@pytest.fixture
def fake(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCHESTRATOR_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("JIRA_API_TOKEN", TOKEN)
    with FakeJira(recent_world(), token=TOKEN) as f:
        yield f


def args(fake, tmp, *extra):
    return [*extra, "--base-url", fake.url, "--state-dir", str(tmp / "state"), "--ttl", "0"]


def poll_once(fake, tmp):
    assert program().main(args(fake, tmp, "--once", "--events-file", str(tmp / "events.jsonl"))) == 0
    return [json.loads(x) for x in (tmp / "events.jsonl").read_text().splitlines()]


def changes(events):
    return [(e["type"], e["subject"], e["source"], e["data"]["to"]) for e in events
            if e["type"] != "orchestra.machine.started"]


def test_two_polls_relay_each_change_once(fake, tmp_path):
    assert changes(poll_once(fake, tmp_path)) == [("jira.issue.transitioned", "ABC-2", "person.jira.bob", "In Progress")]
    fake.move("ABC-1", "In Progress", by="alice")
    events = poll_once(fake, tmp_path)
    assert changes(events)[1:] == [("jira.issue.transitioned", "ABC-1", "person.jira.alice", "In Progress")]
    assert {e["source"] for e in events if e["type"] == "orchestra.machine.started"} == {"person.jira.bob",
                                                                                     "person.jira.alice"}
    assert all(r.authorized for r in fake.requests)


def test_measure_counts_by_status(fake, tmp_path, capsys):
    assert program().main(args(fake, tmp_path, "measure")) == 0
    out = capsys.readouterr().out
    assert 'jira_issues{category="new",status="Open"} 1' in out
    assert 'jira_issues{category="indeterminate",status="In Progress"} 1' in out


def test_no_text_or_token_leaves(fake, tmp_path, capsys):
    poll_once(fake, tmp_path)
    program().main(args(fake, tmp_path, "measure"))
    out = capsys.readouterr()
    assert CANARY not in out.out + out.err and TOKEN not in out.out + out.err
    for p in tmp_path.rglob("*"):
        if p.is_file():
            text = p.read_text(errors="replace")
            assert CANARY not in text and TOKEN not in text, p
