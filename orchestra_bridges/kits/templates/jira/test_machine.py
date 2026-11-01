"""{{name}} against the kit's Jira fake: two polls (a restart between them) relay each status
change once, as who made it; ``measure`` prints the issues by status; no text or token leaves.

Run: ``uv run python -m pytest {{dir}}/test_{{name}}.py`` (in an environment with orchestra-bridges)."""

import importlib.util
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from orchestra_bridges.kits.fakes import CANARY, FakeJira
from orchestra_bridges.kits.fakes.base import load_fixture
from orchestra_bridges.kits.fakes.jira import stamp
from orchestra_bridges.kits.testing import set_args, settings, token_env

HERE = Path(__file__).resolve().parent
TOKEN = "test-token-not-real"
TOKEN_ENV = token_env(HERE, "JIRA_API_TOKEN")        # machine.toml's, so the owner's variable works too
# The fake holds one project: the first machine.toml's JQL names (ABC when it names none), and the
# run is given `project = <it>` (the rest of the owner's JQL is theirs; the fake knows only projects).
_named = re.search(r'(?i)\bproject\s*(?:=|in\s*\()\s*"?([A-Z][A-Z0-9_]*)', settings(HERE).get("jql", ""))
PROJECT = _named.group(1) if _named and _named.group(1) != "OPS" else "ABC"   # OPS: the fixture's out-of-scope one
SCOPE = set_args(jql=f"project = {PROJECT}")


def program():
    spec = importlib.util.spec_from_file_location("{{name}}", HERE / "{{name}}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def recent_world() -> dict:
    """The kit's fixture, its times moved to the last hours (the poller reads back a day)."""
    world, now = load_fixture(None, "jira.json"), datetime.now(UTC)
    for n, issue in enumerate(world["issues"]):
        if issue["project"] == "ABC":                # the fixture's project in scope (OPS stays out)
            issue["key"], issue["project"] = f"{PROJECT}-{issue['key'].split('-')[1]}", PROJECT
        t = stamp(now - timedelta(hours=3 - n))
        issue["updated"] = t
        for h in issue.get("history") or []:
            h["at"] = t
    return world


@pytest.fixture
def fake(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCHESTRATOR_HOME", str(tmp_path / "home"))
    monkeypatch.setenv(TOKEN_ENV, TOKEN)
    with FakeJira(recent_world(), token=TOKEN) as f:
        yield f


def args(fake, tmp, *extra):
    return [*extra, "--base-url", fake.url, "--state-dir", str(tmp / "state"), "--ttl", "0", *SCOPE]


def poll_once(fake, tmp):
    assert program().main(args(fake, tmp, "--once", "--events-file", str(tmp / "events.jsonl"))) == 0
    return [json.loads(x) for x in (tmp / "events.jsonl").read_text().splitlines()]


def changes(events):
    return [(e["type"], e["subject"], e["source"], e["data"]["to"]) for e in events
            if e["type"] != "orchestra.machine.started"]


def test_two_polls_relay_each_change_once(fake, tmp_path):
    assert changes(poll_once(fake, tmp_path)) == [("jira.issue.transitioned", f"{PROJECT}-2", "person.jira.bob",
                                                    "In Progress")]
    fake.move(f"{PROJECT}-1", "In Progress", by="alice")
    events = poll_once(fake, tmp_path)
    assert changes(events)[1:] == [("jira.issue.transitioned", f"{PROJECT}-1", "person.jira.alice",
                                                  "In Progress")]
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
