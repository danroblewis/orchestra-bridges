"""slack_example against the kit's Slack fake: two polls (a restart between them) relay each thread and
message once, as who acted; a check and its removal; ``measure`` prints the channel's counts; no
text or token leaves.

Run with Orchestra's own Python (the one ``orchestra`` runs in, which has the kits): ``python -m pytest .orchestra/machines/slack_example/test_slack_example.py``."""

import importlib.util
import json
import time
from pathlib import Path

import pytest

from orchestra_bridges.kits.fakes import CANARY, FakeSlack
from orchestra_bridges.kits.fakes.base import load_fixture
from orchestra_bridges.kits.testing import fake_token, set_args, settings, token_env

HERE = Path(__file__).resolve().parent
TOKEN = fake_token(HERE)        # the placeholder the runner sends a fake on this machine
REAL = "real-token-never-sent-to-a-fake"   # in the environment, yet never read for a fake
TOKEN_ENV = token_env(HERE, "SLACK_TOKEN")           # poller.toml's, so the owner's variable works too
CHANNEL = (settings(HERE).get("channels") or ["C001"])[0]   # the fake holds poller.toml's first channel
SCOPE = set_args(channels=[CHANNEL], since="1d")


def program():
    spec = importlib.util.spec_from_file_location("slack_example", HERE / "slack_example.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def recent_world() -> dict:
    """The kit's fixture, its messages moved to the last minutes (the poller reads back a day)."""
    world, now = load_fixture(None, "slack.json"), time.time()
    msgs = world["channels"].pop("C001")
    world["channels"][CHANNEL] = msgs
    world["names"] = {CHANNEL: "general"}
    stamps = {m["ts"]: f"{now - 600 + 60 * n:.6f}" for n, m in enumerate(msgs)}
    for m in msgs:
        m["ts"] = stamps[m["ts"]]
        if "thread_ts" in m:
            m["thread_ts"] = stamps[m["thread_ts"]]
    return world


@pytest.fixture
def fake(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCHESTRATOR_HOME", str(tmp_path / "home"))
    monkeypatch.setenv(TOKEN_ENV, REAL)
    with FakeSlack(recent_world(), token=TOKEN) as f:
        yield f


def args(fake, tmp, *extra):
    return [*extra, "--base-url", fake.url, "--state-dir", str(tmp / "state"), "--ttl", "0", *SCOPE]


def poll_once(fake, tmp):
    assert program().main(args(fake, tmp, "--once", "--events-file", str(tmp / "events.jsonl"))) == 0
    return [json.loads(x) for x in (tmp / "events.jsonl").read_text().splitlines()]


def posted(events):
    return [(e["source"], e["data"]["reply"]) for e in events if e["type"] == "slack.message.posted"]


def threads(events):
    return [(e["type"].rsplit(".", 1)[1], e["source"]) for e in events if e["type"].startswith("slack.thread.")]


def test_two_polls_relay_each_thread_and_message_once(fake, tmp_path):
    events = poll_once(fake, tmp_path)
    assert posted(events) == [("person.slack.u001", False), ("person.slack.u002", True), ("bot.slack.b001", False)]
    assert threads(events) == [("started", "person.slack.u001"), ("replied", "person.slack.u002"),
                               ("started", "bot.slack.b001")]
    replied = next(e for e in events if e["type"] == "slack.thread.replied")
    assert replied["data"]["first"] and not replied["data"]["by_author"] and replied["data"]["channel_name"] == "general"
    parent = replied["subject"].split("/")[1]
    fake.post(CHANNEL, "U002")
    fake.react(CHANNEL, parent, "U002")
    events = poll_once(fake, tmp_path)
    assert posted(events)[3:] == [("person.slack.u002", False)]
    assert threads(events)[3:] == [("checked", "person.slack.u002"), ("started", "person.slack.u002")]
    fake.unreact(CHANNEL, parent, "U002")
    events = poll_once(fake, tmp_path)
    assert threads(events)[5:] == [("unchecked", "person.slack.u002")]      # only who checked it can remove it
    unchecked = next(e for e in events if e["type"] == "slack.thread.unchecked")
    assert unchecked["data"]["by"] == "U002"
    assert unchecked["data"]["permalink"].endswith(f"/archives/{CHANNEL}/p{parent.replace('.', '')}")
    fake.react(CHANNEL, parent, "U001")
    fake.react(CHANNEL, parent, "U002")
    poll_once(fake, tmp_path)
    fake.unreact(CHANNEL, parent, "U001")
    fake.unreact(CHANNEL, parent, "U002")
    events = poll_once(fake, tmp_path)                                         # both removed: whose was last?
    assert threads(events)[-1] == ("unchecked", "person.slack.u001")         # the thread's author
    assert events[-1]["data"]["by"] == "unknown"
    assert not any(e["source"] == "bot.slack" for e in events)               # never a source of its own
    assert all(r.authorized for r in fake.requests)


def test_measure_counts_the_channel(fake, tmp_path, capsys):
    assert program().main(args(fake, tmp_path, "measure")) == 0
    out = capsys.readouterr().out
    assert 'slack_channel_messages_last_hour{channel="general"} 2' in out
    assert 'slack_threads_awaiting_reply{channel="general"} 1' in out     # the bot's, unanswered
    assert 'slack_threads_unchecked{channel="general"} 2' in out


def test_no_text_or_token_leaves(fake, tmp_path, capsys):
    poll_once(fake, tmp_path)
    program().main(args(fake, tmp_path, "measure"))
    out = capsys.readouterr()
    assert CANARY not in out.out + out.err and REAL not in out.out + out.err
    for p in tmp_path.rglob("*"):
        if p.is_file():
            text = p.read_text(errors="replace")
            assert CANARY not in text and REAL not in text, p
