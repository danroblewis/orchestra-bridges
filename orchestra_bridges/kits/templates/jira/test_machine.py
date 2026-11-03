"""{{name}} against the kit's Jira fake, in the edition poller.toml names (Data Center or Cloud):
two polls (a restart between them) relay each creation, assignment, status change and comment once, as who
made it; the first run starts now (or at ``since``: then only the issues not yet done), never at the
beginning of history; ``measure`` prints the issues by status; the fake gets only the runner's placeholders,
never a real token; no text or token leaves.

Run with Orchestra's own Python (the one ``orchestra`` runs in, which has the kits): ``python -m pytest {{dir}}/test_{{name}}.py``."""

import importlib.util
import json
import re
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from orchestra_bridges.kits.fakes import CANARY, FakeJira
from orchestra_bridges.kits.fakes.base import load_fixture
from orchestra_bridges.kits.fakes.jira import stamp
from orchestra_bridges.kits.machinefile import read
from orchestra_bridges.kits.testing import fake_token, fake_user, set_args, settings, token_env, uncleared

HERE = Path(__file__).resolve().parent
TOKEN = fake_token(HERE)        # the placeholder the runner sends a fake on this machine
REAL = "real-token-never-sent-to-a-fake"   # in the environment, yet never read for a fake
TOKEN_ENV = token_env(HERE, "JIRA_API_TOKEN")        # poller.toml's, so the owner's variable works too
# The fake holds one project: the first poller.toml's JQL names (ABC when it names none), and the
# run is given `project = <it>` (the rest of the owner's JQL is theirs; the fake knows only projects).
_named = re.search(r'(?i)\bproject\s*(?:=|in\s*\()\s*"?([A-Z][A-Z0-9_]*)', settings(HERE).get("jql", ""))
PROJECT = _named.group(1) if _named and _named.group(1) != "OPS" else "ABC"   # OPS: the fixture's out-of-scope one
SCOPE = set_args(jql=f"project = {PROJECT}", since="now", users=[], sprint_field="")
CATCH_UP = set_args(jql=f"project = {PROJECT}", since="1d", users=[], sprint_field="")     # the first run reads the last day


def program(here=HERE):
    spec = importlib.util.spec_from_file_location("{{name}}", here / "{{name}}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


EDITION = program().edition(read(HERE / "poller.toml"))     # the owner's site decides: cloud or datacenter


def recent_world() -> dict:
    """The kit's fixture, its times moved to the last hours (the poller reads back a day)."""
    world, now = load_fixture(None, "jira.json"), datetime.now(UTC)
    for n, issue in enumerate(world["issues"]):
        if issue["project"] == "ABC":                # the fixture's project in scope (OPS stays out)
            issue["key"], issue["project"] = f"{PROJECT}-{issue['key'].split('-')[1]}", PROJECT
        t = stamp(now - timedelta(days=3, hours=-n))   # created before the first poll's day
        issue["created"] = t
        issue["updated"] = stamp(now - timedelta(hours=3 - n))
        for h in issue.get("history") or []:
            h["at"] = issue["updated"]
    return world


@pytest.fixture
def fake(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCHESTRATOR_HOME", str(tmp_path / "home"))
    monkeypatch.setenv(TOKEN_ENV, REAL)
    with FakeJira(recent_world(), token=TOKEN, basic_user=fake_user(HERE), edition=EDITION) as f:
        yield f


def args(fake, tmp, *extra, scope=SCOPE):
    return [*extra, "--base-url", fake.url, "--state-dir", str(tmp / "state"), "--ttl", "0", *scope]


def poll_once(fake, tmp, scope=SCOPE):
    assert program().main(args(fake, tmp, "--once", "--events-file", str(tmp / "events.jsonl"), scope=scope)) == 0
    log = tmp / "events.jsonl"                      # none published: no file
    return [json.loads(x) for x in log.read_text().splitlines()] if log.exists() else []


NOW = ("status", "category", "issuetype", "labels")      # the issue as the poll read it: on every jira.issue.*


def changes(events):
    """What changed: each event's data without the issue's state now (``now_of``)."""
    return [(e["type"], e["subject"], e["source"], {k: v for k, v in e["data"].items() if k not in NOW})
            for e in events if e["type"] != "orchestra.machine.started"]


def test_two_polls_relay_each_change_once(fake, tmp_path):
    assert changes(poll_once(fake, tmp_path, CATCH_UP)) == [("jira.issue.transitioned", f"{PROJECT}-2", "person.jira.bob",
                                                    {"from": "Open", "to": "In Progress", "replayed": True})]
    # the fixture's change of hours ago, read by the catch-up: done before the run, so marked replayed
    fake.move(f"{PROJECT}-1", "In Progress", by="alice")
    fake.assign(f"{PROJECT}-1", "bob", by="alice")
    fake.create(f"{PROJECT}-9", by="carol", assignee="alice")
    fake.comment(f"{PROJECT}-1", by="carol")
    events = poll_once(fake, tmp_path, CATCH_UP)                        # a restart: the cursor is read back
    assert changes(events)[1:] == [
        ("jira.issue.transitioned", f"{PROJECT}-1", "person.jira.alice", {"from": "Open", "to": "In Progress"}),
        ("jira.issue.assigned", f"{PROJECT}-1", "person.jira.alice", {"assignee": "bob"}),
        ("jira.comment.added", f"{PROJECT}-1", "person.jira.carol", {"issue": f"{PROJECT}-1", "author": "carol"}),
        ("jira.issue.created", f"{PROJECT}-9", "person.jira.carol", {}),
        ("jira.issue.assigned", f"{PROJECT}-9", "person.jira.carol", {"assignee": "alice"})]
    announced = {e["source"]: e["data"] for e in events if e["type"] == "orchestra.machine.started"}
    assert set(announced) == {"person.jira.bob", "person.jira.alice", "person.jira.carol"}     # each actor once
    assert all(a["fake"] for a in announced.values())        # their cards say they were relayed from a fake
    assert not any(e["source"] == "{{name}}" for e in events)          # a relay never publishes as itself
    assert fake.requests and all(r.authorized for r in fake.requests)  # the placeholder, never REAL


def test_the_first_run_starts_now(fake, tmp_path):
    """No cursor and ``since = "now"``: nothing done before the poller began is relayed (the
    fixture's changes of the last hours, a comment), and what is done after it is."""
    fake.comment(f"{PROJECT}-2", by="carol", at=datetime.now(UTC) - timedelta(hours=1))
    assert changes(poll_once(fake, tmp_path)) == []
    fake.move(f"{PROJECT}-1", "In Progress", by="alice")
    assert changes(poll_once(fake, tmp_path)) == [
        ("jira.issue.transitioned", f"{PROJECT}-1", "person.jira.alice", {"from": "Open", "to": "In Progress"})]


def test_a_catch_up_relays_only_the_issues_not_yet_done(fake, tmp_path):
    """``since = "1d"``: an issue already done when the poller first runs is never relayed as
    created or assigned; once the poller runs, its later changes are."""
    hours_ago = datetime.now(UTC) - timedelta(hours=2)
    fake.create(f"{PROJECT}-5", status="Done", by="carol", assignee="alice", at=hours_ago)
    fake.assign(f"{PROJECT}-2", "carol", by="bob", at=hours_ago)
    fake.move(f"{PROJECT}-2", "In Review", by="bob", at=hours_ago)
    fake.move(f"{PROJECT}-2", "Done", by="carol", at=hours_ago)
    assert changes(poll_once(fake, tmp_path, CATCH_UP)) == []
    fake.move(f"{PROJECT}-5", "Open", by="alice")                  # reopened after the poller began
    assert changes(poll_once(fake, tmp_path, CATCH_UP)) == [
        ("jira.issue.transitioned", f"{PROJECT}-5", "person.jira.alice", {"from": "Done", "to": "Open"})]


def test_each_issue_event_carries_the_issue_now_so_a_start_can_refuse_one_already_done(fake, tmp_path):
    """An issue assigned and then moved to Done between two polls: its assignment is relayed with
    the issue as the poll read it (done), its type and its labels."""
    assert changes(poll_once(fake, tmp_path)) == []
    fake.create(f"{PROJECT}-7", by="carol", issuetype="Bug", labels=["prr"])
    fake.assign(f"{PROJECT}-7", "alice", by="carol")
    fake.move(f"{PROJECT}-7", "Done", by="alice")
    got = {e["type"]: e["data"] for e in poll_once(fake, tmp_path) if e.get("subject") == f"{PROJECT}-7"}
    for t in ("jira.issue.created", "jira.issue.assigned", "jira.issue.transitioned"):
        assert {k: got[t][k] for k in NOW} == {"status": "Done", "category": "done", "issuetype": "Bug",
                                                "labels": ["prr"]}, t
    assert got["jira.issue.assigned"]["assignee"] == "alice"


def test_a_team_scope_and_the_sprint(fake, tmp_path):
    """``users`` keeps the team's issues (assigned to or reported by one of them); ``sprint_field``
    puts the issue's open sprint's name on each event, in this edition's shape."""
    team = set_args(jql=f"project = {PROJECT}", since="now", users=["dana", "erin"], sprint_field="customfield_10101")
    poll_once(fake, tmp_path, team)
    sprint = {"id": 7, "name": "Team Sprint 7", "state": "ACTIVE"}
    fake.create(f"{PROJECT}-20", by="dana", assignee="zed", issuetype="Epic", sprint=sprint)   # reported by the team
    fake.create(f"{PROJECT}-21", by="zed", assignee="erin", issuetype="UFF")                 # assigned to the team
    fake.create(f"{PROJECT}-22", by="zed", assignee="zed", sprint=sprint)                    # not the team's
    made = {e["subject"]: e["data"] for e in poll_once(fake, tmp_path, team) if e["type"] == "jira.issue.created"}
    assert set(made) == {f"{PROJECT}-20", f"{PROJECT}-21"}
    assert made[f"{PROJECT}-20"]["issuetype"] == "Epic" and made[f"{PROJECT}-20"]["sprint"] == "Team Sprint 7"
    assert made[f"{PROJECT}-21"]["issuetype"] == "UFF" and made[f"{PROJECT}-21"]["sprint"] is None
    searched = [r for r in fake.requests if r.path.endswith("/search") or r.path.endswith("/search/jql")]
    assert all("assignee in" in (r.query.get("jql") or [""])[0] for r in searched)


def test_measure_counts_by_status(fake, tmp_path, capsys):
    assert program().main(args(fake, tmp_path, "measure")) == 0
    out = capsys.readouterr().out
    assert 'jira_issues{category="new",status="Open"} 1' in out
    assert 'jira_issues{category="indeterminate",status="In Progress"} 1' in out


def test_no_text_or_token_leaves(fake, tmp_path, capsys):
    fake.comment(f"{PROJECT}-2", by="carol")                         # its body carries the canary
    assert any(e["type"] == "jira.comment.added" for e in poll_once(fake, tmp_path, CATCH_UP))
    program().main(args(fake, tmp_path, "measure"))
    out = capsys.readouterr()
    assert CANARY not in out.out + out.err and REAL not in out.out + out.err
    for p in tmp_path.rglob("*"):
        if p.is_file():
            text = p.read_text(errors="replace")
            assert CANARY not in text and REAL not in text, p


def test_refuses_an_uncleared_system(tmp_path, monkeypatch):
    monkeypatch.setenv(TOKEN_ENV, REAL)
    copy = uncleared(HERE, tmp_path / "copy", "{{name}}.py")      # holds after the owner's approval too
    rc = program(copy).main(["--once", "--base-url", "https://jira.example.com", "--state-dir", str(tmp_path / "s"),
                             "--events-file", str(tmp_path / "e.jsonl")])
    assert rc == 2 and not (tmp_path / "e.jsonl").exists()
