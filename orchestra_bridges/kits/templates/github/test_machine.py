"""{{name}} against the kit's GitHub fake: three polls (a restart between each) relay each issue's and
pull request's change once, as who made it (a push, a label, a review, the checks, a merge); a
filter of branches or labels follows only those pull requests; ``measure`` prints the open issues
and the open pull requests by review state; the fake gets only the runner's placeholder, never a
real token; no text or token leaves.

Run with Orchestra's own Python (the one ``orchestra`` runs in, which has the kits): ``python -m pytest {{dir}}/test_{{name}}.py``."""

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
TOKEN_ENV = token_env(HERE, "GITHUB_TOKEN")          # machine.toml's, so the owner's variable works too
REPO = settings(HERE).get("repo") or "acme/service"  # the fake serves the owner's repository's name
SCOPE = set_args(branches=[], labels=[], issues=True)   # the fake's world: follow everything


def program(here=HERE):
    spec = importlib.util.spec_from_file_location("{{name}}", here / "{{name}}.py")
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
    return [json.loads(x) for x in (tmp / "events.jsonl").read_text().splitlines()]


def changes(events):
    return [(e["type"], e["subject"], e["source"]) for e in events if e["type"] != "orchestra.machine.started"]


def test_polls_relay_each_change_once(fake, tmp_path):
    first = changes(poll_once(fake, tmp_path))
    pr1, pr2, i3, i4 = (f"{REPO}#{n}" for n in (1, 2, 3, 4))
    assert sorted(first) == sorted([
        ("github.pr.opened", pr1, "person.github.alice"), ("github.pr.labeled", pr1, "person.github.alice"),
        ("github.checks.completed", pr1, "bot.github.github-actions"),
        ("github.pr.opened", pr2, "person.github.bob"), ("github.pr.reviewed", pr2, "person.github.carol"),
        ("github.issue.opened", i3, "person.github.carol"), ("github.issue.labeled", i3, "person.github.carol"),
        ("github.issue.opened", i4, "bot.github.dependabot"), ("github.issue.closed", i4, "person.github.bob")])
    fake.push(1, "c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3")
    fake.check("c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3c3", "failure")
    fake.check("b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2b2", "success")      # PR 2's checks finish
    fake.label(2, "SAIL", by="bob")
    fake.close(3, by="alice")
    events = poll_once(fake, tmp_path)                                  # a restart: the cursor is read back
    second = changes(events)[len(first):]
    assert sorted(second) == sorted([
        ("github.pr.synchronize", pr1, "person.github.alice"),
        ("github.checks.completed", pr1, "bot.github.github-actions"),
        ("github.checks.completed", pr2, "bot.github.github-actions"),
        ("github.pr.labeled", pr2, "person.github.bob"), ("github.issue.closed", i3, "person.github.alice")])
    fake.merge(2, by="carol")
    events = poll_once(fake, tmp_path)
    assert changes(events)[len(first) + len(second):] == [("github.pr.merged", pr2, "person.github.carol")]
    outcome = {e["data"]["head_sha"][:2]: e["data"]["outcome"] for e in events if e["type"] == "github.checks.completed"}
    assert outcome == {"a1": "success", "c3": "failure", "b2": "success"}
    announced = {e["source"]: e["data"] for e in events if e["type"] == "orchestra.machine.started"}
    assert announced["bot.github.github-actions"]["kind"] == "program" and announced["person.github.bob"]["kind"] == "person"
    assert all(a["fake"] for a in announced.values())        # their cards say they were relayed from a fake
    assert len(announced) == len([e for e in events if e["type"] == "orchestra.machine.started"])   # each once
    assert not any(e["source"] == "{{name}}" for e in events)          # a relay never publishes as itself
    assert fake.requests and all(r.authorized for r in fake.requests)  # the placeholder, never REAL


def test_follows_only_the_branches_or_labels_named(fake, tmp_path):
    got = changes(poll_once(fake, tmp_path, scope=set_args(branches=["fix/*"], labels=[], issues=False)))
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
    copy = uncleared(HERE, tmp_path / "copy", "{{name}}.py")      # holds after the owner's approval too
    rc = program(copy).main(["--once", "--base-url", "https://api.github.com", "--state-dir", str(tmp_path / "s"),
                             "--events-file", str(tmp_path / "e.jsonl")])
    assert rc == 2 and not (tmp_path / "e.jsonl").exists()
