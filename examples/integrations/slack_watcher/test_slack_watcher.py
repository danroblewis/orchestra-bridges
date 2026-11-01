"""slack_watcher against the kit's Slack fake: two polls (a restart between them) relay each message
once, as who posted it; ``measure`` prints the messages per channel; no text or token leaves.

Run: ``uv run python -m pytest examples/integrations/slack_watcher/test_slack_watcher.py`` (in an environment with orchestra-bridges)."""

import importlib.util
import json
import time
from pathlib import Path

import pytest

from orchestra_bridges.kits.fakes import CANARY, FakeSlack
from orchestra_bridges.kits.fakes.base import load_fixture

HERE = Path(__file__).resolve().parent
TOKEN = "test-token-not-real"


def program():
    spec = importlib.util.spec_from_file_location("slack_watcher", HERE / "slack_watcher.py")
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


@pytest.fixture
def fake(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCHESTRATOR_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("SLACK_TOKEN", TOKEN)
    with FakeSlack(recent_world(), token=TOKEN) as f:
        yield f


def args(fake, tmp, *extra):
    return [*extra, "--base-url", fake.url, "--state-dir", str(tmp / "state"), "--ttl", "0"]


def poll_once(fake, tmp):
    assert program().main(args(fake, tmp, "--once", "--events-file", str(tmp / "events.jsonl"))) == 0
    return [json.loads(x) for x in (tmp / "events.jsonl").read_text().splitlines()]


def posted(events):
    return [(e["source"], e["data"]["reply"]) for e in events if e["type"] == "slack.message.posted"]


def test_two_polls_relay_each_message_once(fake, tmp_path):
    assert posted(poll_once(fake, tmp_path)) == [("person.slack.u001", False), ("person.slack.u002", True),
                                                 ("bot.slack.b001", False)]
    fake.post("C001", "U002")
    events = poll_once(fake, tmp_path)
    assert posted(events)[3:] == [("person.slack.u002", False)]
    assert all(r.authorized for r in fake.requests)


def test_measure_counts_the_last_hour(fake, tmp_path, capsys):
    assert program().main(args(fake, tmp_path, "measure")) == 0
    assert 'slack_channel_messages_last_hour{channel="C001"} 3' in capsys.readouterr().out


def test_no_text_or_token_leaves(fake, tmp_path, capsys):
    poll_once(fake, tmp_path)
    program().main(args(fake, tmp_path, "measure"))
    out = capsys.readouterr()
    assert CANARY not in out.out + out.err and TOKEN not in out.out + out.err
    for p in tmp_path.rglob("*"):
        if p.is_file():
            text = p.read_text(errors="replace")
            assert CANARY not in text and TOKEN not in text, p
