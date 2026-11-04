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
from orchestra_bridges.kits.testing import down_for, fake_token, forget_emitted, set_args, settings, token_env

HERE = Path(__file__).resolve().parent
TOKEN = fake_token(HERE)        # the placeholder the runner sends a fake on this machine
REAL = "real-token-never-sent-to-a-fake"   # in the environment, yet never read for a fake
TOKEN_ENV = token_env(HERE, "SLACK_TOKEN")           # poller.toml's, so the owner's variable works too
CHANNEL = (settings(HERE).get("channels") or ["C001"])[0]   # the fake holds poller.toml's first channel
SCOPE = set_args(channels=[CHANNEL], since="1d", cadence={}, max_backoff="1h")


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


def args(fake, tmp, *extra, scope=SCOPE):
    return [*extra, "--base-url", fake.url, "--state-dir", str(tmp / "state"), "--ttl", "0", *scope]


def poll_once(fake, tmp, scope=SCOPE):
    assert program().main(args(fake, tmp, "--once", "--events-file", str(tmp / "events.jsonl"), scope=scope)) == 0
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


def test_a_thread_started_while_the_poller_was_down_is_new_after_the_restart(fake, tmp_path):
    """The last poll, a thread started, the poller down well over an interval, a restart: the first
    poll after it reads from the saved cursor and publishes the thread once, as new."""
    before = len(poll_once(fake, tmp_path))
    down_for(tmp_path / "state", 2 * 3600, "seen")                      # the last poll two hours ago
    ts = fake.post(CHANNEL, "U009", ts=f"{time.time() - 3600:.6f}")      # an hour ago, while down
    got = [e for e in poll_once(fake, tmp_path)[before:] if e.get("subject") == f"{CHANNEL}/{ts}"]
    assert [e["type"] for e in got] == ["slack.thread.started", "slack.message.posted"]
    assert not any("replayed" in e["data"] for e in got)
    assert len([e for e in poll_once(fake, tmp_path) if e.get("subject") == f"{CHANNEL}/{ts}"]) == 2   # once


def test_a_thread_published_before_a_restart_is_a_replay_after_it(fake, tmp_path):
    """Published before the stop: after the restart the channel's cursor has passed it (never read
    again) and its id is known. Its id lost and the channel read again from ``since`` (dropped from
    the list and added back): known by the mark the last good poll left, relayed marked replayed."""
    poll_once(fake, tmp_path)                                          # the first run's catch-up
    ts = fake.post(CHANNEL, "U009")
    def started(events):
        return [e["data"] for e in events if e["type"] == "slack.thread.started" and e["subject"] == f"{CHANNEL}/{ts}"]
    assert [d.get("replayed") for d in started(poll_once(fake, tmp_path))] == [None]       # live: new
    time.sleep(1.1)                     # the next good poll's mark, in whole seconds, after it
    assert len(started(poll_once(fake, tmp_path))) == 1                                  # not again
    forget_emitted(tmp_path / "state")
    poll_once(fake, tmp_path, set_args(channels=[], since="1d", cadence={}, max_backoff="1h"))   # dropped
    assert [d.get("replayed") for d in started(poll_once(fake, tmp_path))] == [None, True]   # added back


def read_of(fake, channel):
    return [r for r in fake.requests if r.path == "/api/conversations.history" and r.query.get("channel") == [channel]]


def test_a_channel_on_its_own_cadence_is_read_only_when_due(fake, tmp_path):
    fake.data["channels"]["C0QUIET"], fake.data["names"]["C0QUIET"] = [], "quiet"
    scope = set_args(channels=[CHANNEL, "C0QUIET"], since="1d", cadence={"C0QUIET": "1h"}, max_backoff="1h")
    poll_once(fake, tmp_path, scope)
    poll_once(fake, tmp_path, scope)                       # a restart, at once: the quiet one is not due
    assert len(read_of(fake, CHANNEL)) == 2 and len(read_of(fake, "C0QUIET")) == 1


def test_a_rate_limited_channel_backs_off_and_the_others_go_on(fake, tmp_path, capsys):
    fake.data["channels"]["C0BUSY"], fake.data["names"]["C0BUSY"] = [], "busy"
    scope = set_args(channels=["C0BUSY", CHANNEL], since="1d", cadence={}, max_backoff="1h")
    fake.fail_next(429, {"Retry-After": "3600"})           # longer than the client waits: the read fails
    events = poll_once(fake, tmp_path, scope)               # the poll itself succeeds
    assert ("started", "person.slack.u001") in threads(events)            # the other channel was read
    assert "C0BUSY rate limited (429)" in capsys.readouterr().err
    cursor = json.loads(next((tmp_path / "state").rglob("cursor.json")).read_text())
    assert cursor["cursor"]["C0BUSY"]["backoff"] >= 1 and cursor["cursor"]["C0BUSY"]["due"] > time.time()
    before = len(fake.requests)
    poll_once(fake, tmp_path, scope)                        # backing off: the busy channel is not read
    assert not [r for r in fake.requests[before:] if r.query.get("channel") == ["C0BUSY"]]


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
