"""jira_poller against the kit's Jira fake: two intervals of the running poller relay each creation,
status change and other change once, as who made it; a restart publishes nothing again; ``measure``
prints the issues and the oldest's age by status and the moves between statuses; it reads only a
fake until cleared; no text or token leaves.

Run: ``uv run python -m pytest examples/integrations/jira_poller/test_jira_poller.py`` (in an
environment with orchestra-bridges)."""

import importlib.util
import json
import os
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from orchestra_bridges.kits.fakes import CANARY, FakeJira
from orchestra_bridges.kits.fakes.base import load_fixture
from orchestra_bridges.kits.fakes.jira import stamp

HERE = Path(__file__).resolve().parent
PROGRAM = HERE / "jira_poller.py"
TOKEN = "test-token-not-real"
STARTED = "orchestra.machine.started"


def program():
    spec = importlib.util.spec_from_file_location("jira_poller", PROGRAM)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def recent_world() -> dict:
    """The kit's fixture, its times moved to the last hours (the poller reads back a day)."""
    world, now = load_fixture(None, "jira.json"), datetime.now(UTC)
    for n, issue in enumerate(world["issues"]):
        issue["created"] = stamp(now - timedelta(hours=5 - n))
        t = stamp(now - timedelta(hours=3 - n))
        issue["updated"] = t
        for h in issue.get("history") or []:
            h["at"] = t
    return world


def env(tmp, **extra):
    e = {k: v for k, v in os.environ.items() if k != "JIRA_API_TOKEN"}
    return {**e, "ORCHESTRATOR_HOME": str(tmp / "home"), **extra}


def common(fake, tmp):
    return ["--base-url", fake.url, "--state-dir", str(tmp / "state"), "--ttl", "0"]


def events_of(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


def relayed(events):
    return [(e["type"], e["subject"], e["source"]) for e in events if e["type"] != STARTED]


def wait_for(path: Path, n: int, timeout: float = 30.0) -> list[dict]:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if len(relayed(events_of(path))) >= n:
            return events_of(path)
        time.sleep(0.05)
    raise AssertionError(f"fewer than {n} events: {events_of(path)}")


def run_two_intervals(fake, tmp, change, **extra_env):
    """The poller as a process, every 2 s for two polls; ``change`` alters the world between them."""
    ev = tmp / "events.jsonl"
    proc = subprocess.Popen([sys.executable, str(PROGRAM), "--every", "2", "--rounds", "2", "--events-file", str(ev),
                             *common(fake, tmp)], env=env(tmp, **extra_env), stderr=subprocess.PIPE, text=True)
    try:
        wait_for(ev, 3)
        change(fake)
        assert proc.wait(timeout=30) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    return events_of(ev), proc.stderr.read()


def change(fake):
    fake.create("ABC-3", "Open", by="dave")
    fake.move("ABC-1", "In Progress", by="alice")
    fake.edit("ABC-2", "priority", by="bob")


@pytest.fixture
def fake():
    with FakeJira(recent_world()) as f:                 # the fake needs no token
        yield f


def test_two_intervals_relay_each_change_once_and_a_restart_adds_nothing(fake, tmp_path):
    events, log = run_two_intervals(fake, tmp_path, change)
    got = relayed(events)
    assert got[:3] == [("jira.issue.created", "ABC-1", "person.jira.alice"),         # interval 1
                       ("jira.issue.created", "ABC-2", "person.jira.bob"),
                       ("jira.issue.transitioned", "ABC-2", "person.jira.bob")]
    assert sorted(got[3:]) == [("jira.issue.created", "ABC-3", "person.jira.dave"),   # interval 2 (one second: any order)
                               ("jira.issue.transitioned", "ABC-1", "person.jira.alice"),
                               ("jira.issue.updated", "ABC-2", "person.jira.bob")]
    by_type = {(e["type"], e.get("subject")): e for e in events}
    assert by_type[("jira.issue.transitioned", "ABC-1")]["data"] == {"from": "Open", "to": "In Progress"}
    assert by_type[("jira.issue.updated", "ABC-2")]["data"] == {"fields": ["priority"]}
    announced = [e["source"] for e in events if e["type"] == STARTED]
    assert sorted(announced) == ["person.jira.alice", "person.jira.bob", "person.jira.dave"]   # each actor once
    assert not any(e["source"] == "jira_poller" for e in events)       # a relay never publishes as itself
    assert "poll 2" in log and "stopped" in log

    rc = program().main(["--once", "--events-file", str(tmp_path / "events.jsonl"), *common(fake, tmp_path)])
    assert rc == 0 and events_of(tmp_path / "events.jsonl") == events  # a restart: nothing twice


def test_measure_prints_the_issues_their_age_and_the_moves(fake, tmp_path, capsys):
    run_two_intervals(fake, tmp_path, change)
    assert program().main(["measure", *common(fake, tmp_path)]) == 0
    out = capsys.readouterr().out
    assert 'jira_issues_total{status="In Progress"} 2' in out and 'jira_issues_total{status="Open"} 1' in out
    assert 'jira_issues_created_total{status="Open"} 3' in out
    assert 'jira_issue_transitions_total{from_status="Open",to_status="In Progress"} 2' in out
    assert "jira_issue_transitions_total{" in out and out.count("jira_issue_transitions_total{") == 1
    ages = {line.split("{")[1].split("}")[0]: float(line.rsplit(" ", 1)[1]) for line in out.splitlines()
            if line.startswith("jira_issue_age_seconds{")}
    assert 5 * 3600 - 60 < ages['status="In Progress"'] < 5 * 3600 + 60      # ABC-1, created 5 h ago
    assert ages['status="Open"'] < 60                                          # ABC-3, just now


def test_a_status_emptied_reads_zero(fake, tmp_path, capsys):
    run_two_intervals(fake, tmp_path, change)
    fake.move("ABC-3", "In Progress", by="dave")
    assert program().main(["--once", "--events-file", str(tmp_path / "events.jsonl"), *common(fake, tmp_path)]) == 0
    assert program().main(["measure", *common(fake, tmp_path)]) == 0
    assert 'jira_issues_total{status="Open"} 0' in capsys.readouterr().out


def test_no_text_or_token_leaves(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("JIRA_API_TOKEN", TOKEN)
    monkeypatch.setenv("ORCHESTRATOR_HOME", str(tmp_path / "home"))
    with FakeJira(recent_world(), token=TOKEN) as f:
        _, log = run_two_intervals(f, tmp_path, change, JIRA_API_TOKEN=TOKEN)
        assert program().main(["measure", *common(f, tmp_path)]) == 0
        assert f.requests and all(r.authorized for r in f.requests)
    out = capsys.readouterr()
    for text in (out.out, out.err, log):
        assert CANARY not in text and TOKEN not in text
    for p in tmp_path.rglob("*"):
        if p.is_file():
            text = p.read_text(errors="replace")
            assert CANARY not in text and TOKEN not in text, p


def test_reads_only_a_fake_until_cleared(tmp_path, monkeypatch):
    monkeypatch.setenv("JIRA_API_TOKEN", TOKEN)
    rc = program().main(["--once", "--base-url", "https://jira.example.com", "--state-dir", str(tmp_path / "s"),
                         "--events-file", str(tmp_path / "e.jsonl")])
    assert rc == 2 and not (tmp_path / "e.jsonl").exists()
