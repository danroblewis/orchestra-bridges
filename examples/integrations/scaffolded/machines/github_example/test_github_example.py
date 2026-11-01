"""github_example against the kit's GitHub fake: three polls (a restart between each) relay each issue's and
pull request's change once, as who made it (a push, a label, a review, the checks, a merge); the
first run starts now (or at ``since``: then only the items still open), never at the beginning of
history; a
filter of branches or labels follows only those pull requests; ``measure`` prints the open issues
and the open pull requests by review state; the fake gets only the runner's placeholder, never a
real token; no text or token leaves.

Run with Orchestra's own Python (the one ``orchestra`` runs in, which has the kits): ``python -m pytest .orchestra/machines/github_example/test_github_example.py``."""

import importlib.util
import json
from pathlib import Path

import pytest

from orchestra_bridges.kits.fakes import CANARY, FakeGitHub
from orchestra_bridges.kits.fakes.base import load_fixture
from orchestra_bridges.kits.testing import fake_token, set_args, settings, token_env, uncleared

HERE = Path(__file__).resolve().parent
TOKEN = fake_token(HERE)        # the placeholder the runner sends a fake on this machine
REAL = "real-token-never-sent-to-a-fake"   # in the environment, yet never read for a fake
TOKEN_ENV = token_env(HERE, "GITHUB_TOKEN")          # poller.toml's, so the owner's variable works too
REPO = settings(HERE).get("repo") or "acme/service"  # the fake serves the owner's repository's name
SCOPE = set_args(branches=[], labels=[], issues=True, since="1d")   # follow everything; the first run reads a day
NOW = set_args(branches=[], labels=[], issues=True, since="now")


def program(here=HERE):
    spec = importlib.util.spec_from_file_location("github_example", here / "github_example.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def world() -> dict:
    return {**load_fixture(None, "github.json"), "repo": REPO}


@pytest.fixture
def fake(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCHESTRATOR_HOME", str(tmp_path / "home"))
    monkeypatch.setenv(TOKEN_ENV, REAL)
    with FakeGitHub(world(), token=TOKEN) as f:
        yield f


def args(fake, tmp, *extra, scope=SCOPE):
    return [*extra, "--base-url", fake.url, "--state-dir", str(tmp / "state"), "--ttl", "0", *scope]


def poll_once(fake, tmp, scope=SCOPE):
    assert program().main(args(fake, tmp, "--once", "--events-file", str(tmp / "events.jsonl"), scope=scope)) == 0
    log = tmp / "events.jsonl"                      # none published: no file
    return [json.loads(x) for x in log.read_text().splitlines()] if log.exists() else []


def changes(events):
    return [(e["type"], e["subject"], e["source"]) for e in events if e["type"] != "orchestra.machine.started"]


def test_polls_relay_each_change_once(fake, tmp_path):
    first = changes(poll_once(fake, tmp_path))
    pr1, pr2, i3, i4 = (f"{REPO}#{n}" for n in (1, 2, 3, 4))
    assert sorted(first) == sorted([
        ("github.pr.opened", pr1, "person.github.alice"), ("github.pr.labeled", pr1, "person.github.alice"),
        ("github.checks.completed", pr1, "bot.github.github-actions"),
        ("github.pr.opened", pr2, "person.github.bob"), ("github.pr.approved", pr2, "person.github.carol"),
        ("github.issue.opened", i3, "person.github.carol"), ("github.issue.labeled", i3, "person.github.carol")])
    assert not [c for c in first if c[1] == i4]                       # closed before: a catch-up passes it over
    fake.push(1, "c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3")
    fake.check("c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3", "failure")
    fake.check("b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2", "success")      # PR 2's checks finish
    fake.label(2, "SAIL", by="bob")
    fake.request_review(1, "carol", by="alice")
    fake.review(1, "CHANGES_REQUESTED", by="carol")
    fake.review(1, "COMMENTED", by="bob")
    fake.close(3, by="alice")
    events = poll_once(fake, tmp_path)                                  # a restart: the cursor is read back
    second = changes(events)[len(first):]
    assert sorted(second) == sorted([
        ("github.pr.synchronize", pr1, "person.github.alice"),
        ("github.checks.completed", pr1, "bot.github.github-actions"),
        ("github.checks.completed", pr2, "bot.github.github-actions"),
        ("github.pr.labeled", pr2, "person.github.bob"), ("github.issue.closed", i3, "person.github.alice"),
        ("github.pr.review_requested", pr1, "person.github.alice"),
        ("github.pr.changes_requested", pr1, "person.github.carol"), ("github.pr.commented", pr1, "person.github.bob")])
    assert next(e for e in events if e["type"] == "github.pr.review_requested")["data"]["reviewer"] == "carol"
    fake.merge(2, by="carol")
    events = poll_once(fake, tmp_path)
    assert changes(events)[len(first) + len(second):] == [("github.pr.merged", pr2, "person.github.carol")]
    outcome = {e["data"]["head_sha"][:2]: e["data"]["outcome"] for e in events if e["type"] == "github.checks.completed"}
    assert outcome == {"a1": "success", "c3": "failure", "b2": "success"}
    announced = {e["source"]: e["data"] for e in events if e["type"] == "orchestra.machine.started"}
    assert announced["bot.github.github-actions"]["kind"] == "program" and announced["person.github.bob"]["kind"] == "person"
    assert all(a["fake"] for a in announced.values())        # their cards say they were relayed from a fake
    assert len(announced) == len([e for e in events if e["type"] == "orchestra.machine.started"])   # each once
    assert not any(e["source"] == "github_example" for e in events)          # a relay never publishes as itself
    assert fake.requests and all(r.authorized for r in fake.requests)  # the placeholder, never REAL


def test_the_first_run_starts_now(fake, tmp_path):
    """No cursor and ``since = "now"``: nothing done before the poller began is relayed (the
    fixture's labels, review and finished checks of the last hours: PR 1's ``SAIL``), and what is done after it is."""
    assert changes(poll_once(fake, tmp_path, NOW)) == []
    fake.label(1, "ready", by="bob")
    fake.review(2, "CHANGES_REQUESTED", by="carol")
    assert sorted(changes(poll_once(fake, tmp_path, NOW))) == sorted([
        ("github.pr.labeled", f"{REPO}#1", "person.github.bob"),
        ("github.pr.changes_requested", f"{REPO}#2", "person.github.carol")])


def test_a_catch_up_relays_only_the_items_still_open(fake, tmp_path):
    """``since = "1d"``: an item already closed when the poller first runs is never relayed; once
    the poller runs, its later changes are."""
    got = changes(poll_once(fake, tmp_path))
    assert not [c for c in got if c[1] == f"{REPO}#4"]               # opened and closed before: passed over
    fake.reopen(4, by="carol")
    assert changes(poll_once(fake, tmp_path))[len(got):] == [("github.issue.reopened", f"{REPO}#4", "person.github.carol")]


def test_follows_only_the_branches_or_labels_named(fake, tmp_path):
    got = changes(poll_once(fake, tmp_path, scope=set_args(branches=["fix/*"], labels=[], issues=False, since="1d")))
    assert {s for _, s, _ in got} == {f"{REPO}#2"}


def test_measure_prints_the_open_items(fake, tmp_path, capsys):
    assert program().main(args(fake, tmp_path, "measure")) == 0
    out = capsys.readouterr().out
    assert "github_issues_open 1" in out
    assert 'github_prs_open{review="approved"} 1' in out and 'github_prs_open{review="awaiting_review"} 1' in out
    assert 'github_prs_open{review="changes_requested"} 0' in out


def test_no_text_or_token_leaves(fake, tmp_path, capsys):
    poll_once(fake, tmp_path)
    program().main(args(fake, tmp_path, "measure"))
    out = capsys.readouterr()
    assert CANARY not in out.out + out.err and REAL not in out.out + out.err
    for p in tmp_path.rglob("*"):
        if p.is_file():
            text = p.read_text(errors="replace")
            assert CANARY not in text and REAL not in text, p


def test_refuses_an_uncleared_system(tmp_path, monkeypatch):
    monkeypatch.setenv(TOKEN_ENV, REAL)
    copy = uncleared(HERE, tmp_path / "copy", "github_example.py")      # holds after the owner's approval too
    rc = program(copy).main(["--once", "--base-url", "https://api.github.com", "--state-dir", str(tmp_path / "s"),
                             "--events-file", str(tmp_path / "e.jsonl")])
    assert rc == 2 and not (tmp_path / "e.jsonl").exists()
