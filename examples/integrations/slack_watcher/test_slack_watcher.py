"""slack_watcher against the kit's Slack fake: two intervals of the running poller relay each message
once, as who posted it; a restart publishes nothing again; mentions of a watched member open and
close as they answer; ``publish = "mentions"`` filters what is published; ``measure`` prints the
messages per channel and the mentions; it reads only a fake until cleared; no text or token leaves.

Run: ``uv run python -m pytest examples/integrations/slack_watcher/test_slack_watcher.py`` (in an
environment with orchestra-bridges)."""

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

import pytest

from orchestra_bridges.kits.fakes import CANARY, FakeSlack
from orchestra_bridges.kits.fakes.base import load_fixture

HERE = Path(__file__).resolve().parent
PROGRAM = HERE / "slack_watcher.py"
TOKEN = "test-token-not-real"
STARTED = "orchestra.machine.started"


def program(path: Path = PROGRAM):
    spec = importlib.util.spec_from_file_location("slack_watcher", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def recent_world() -> dict:
    """The kit's fixture, its messages moved to the last minutes (the poller reads back a day)."""
    world, now = load_fixture(None, "slack.json"), time.time()
    msgs = world["channels"]["C001"]
    stamps = {m["ts"]: f"{now - 600 + 60 * n:.6f}" for n, m in enumerate(msgs)}
    for m in msgs:
        m["ts"] = stamps[m["ts"]]
        if "thread_ts" in m:
            m["thread_ts"] = stamps[m["thread_ts"]]
    return world


def env(tmp, **extra):
    e = {k: v for k, v in os.environ.items() if k != "SLACK_TOKEN"}
    return {**e, "ORCHESTRATOR_HOME": str(tmp / "home"), **extra}


def common(fake, tmp):
    return ["--base-url", fake.url, "--state-dir", str(tmp / "state"), "--ttl", "0"]


def events_of(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


def posted(events):
    return [e for e in events if e["type"] == "slack.message.posted"]


def wait_for(path: Path, n: int, timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if len(posted(events_of(path))) >= n:
            return
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
    """Two mentions of the watched U001 by U002, and U001 answering the first in its thread."""
    first = fake.post("C001", "U002", mentions=["U001"])
    time.sleep(0.01)
    fake.post("C001", "U002", mentions=["U001"])
    time.sleep(0.01)
    fake.post("C001", "U001", thread_ts=first)


@pytest.fixture
def fake():
    with FakeSlack(recent_world()) as f:                # the fake needs no token
        yield f


def test_two_intervals_relay_each_message_once_and_a_restart_adds_nothing(fake, tmp_path):
    events, log = run_two_intervals(fake, tmp_path, change)
    got = [(e["source"], e["data"]["reply"], e["data"]["mentions"], e["data"]["answers"]) for e in posted(events)]
    assert got == [("person.slack.u001", False, [], False), ("person.slack.u002", True, [], False),
                   ("bot.slack.b001", False, [], False),                                  # interval 1
                   ("person.slack.u002", False, ["U001"], False), ("person.slack.u002", False, ["U001"], False),
                   ("person.slack.u001", True, [], True)]                                 # interval 2
    first = posted(events)[3]
    assert posted(events)[5]["subject"] == first["subject"] == f"C001/{first['subject'].split('/')[1]}"
    assert sorted(e["source"] for e in events if e["type"] == STARTED) == [
        "bot.slack.b001", "person.slack.u001", "person.slack.u002"]                      # each actor once
    assert not any(e["source"] == "slack_watcher" for e in events)
    assert "poll 2" in log and "stopped" in log

    rc = program().main(["--once", "--events-file", str(tmp_path / "events.jsonl"), *common(fake, tmp_path)])
    assert rc == 0 and events_of(tmp_path / "events.jsonl") == events                    # a restart: nothing twice


def test_measure_prints_the_messages_and_the_mentions(fake, tmp_path, capsys):
    run_two_intervals(fake, tmp_path, change)
    program().main(["--once", "--events-file", str(tmp_path / "events.jsonl"), *common(fake, tmp_path)])
    assert program().main(["measure", *common(fake, tmp_path)]) == 0
    out = capsys.readouterr().out
    assert 'slack_messages_total{channel="C001"} 6' in out                               # not counted twice
    assert "slack_mentions_total 2" in out and "slack_mentions_answered_total 1" in out
    assert "slack_mentions_open_total 1" in out and "# TYPE slack_mentions_open_total gauge" in out


def test_publish_mentions_filters_to_the_mentions_and_their_answers(fake, tmp_path):
    d = tmp_path / "copy"
    shutil.copytree(HERE, d, ignore=shutil.ignore_patterns("__pycache__"))
    toml = (d / "machine.toml").read_text().replace('publish = "all"', 'publish = "mentions"')
    (d / "machine.toml").write_text(toml)
    ev = tmp_path / "events.jsonl"
    mod = program(d / "slack_watcher.py")
    assert mod.main(["--once", "--events-file", str(ev), *common(fake, tmp_path)]) == 0
    assert posted(events_of(ev)) == []                                                   # no mention yet
    change(fake)
    assert mod.main(["--once", "--events-file", str(ev), *common(fake, tmp_path)]) == 0
    assert [(e["source"], e["data"]["answers"]) for e in posted(events_of(ev))] == [
        ("person.slack.u002", False), ("person.slack.u002", False), ("person.slack.u001", True)]


def test_no_text_or_token_leaves(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("SLACK_TOKEN", TOKEN)
    monkeypatch.setenv("ORCHESTRATOR_HOME", str(tmp_path / "home"))
    with FakeSlack(recent_world(), token=TOKEN) as f:
        _, log = run_two_intervals(f, tmp_path, change, SLACK_TOKEN=TOKEN)
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
    monkeypatch.setenv("SLACK_TOKEN", TOKEN)
    rc = program().main(["--once", "--base-url", "https://slack.com", "--state-dir", str(tmp_path / "s"),
                         "--events-file", str(tmp_path / "e.jsonl")])
    assert rc == 2 and not (tmp_path / "e.jsonl").exists()
