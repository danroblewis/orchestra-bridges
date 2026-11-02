"""The Slack kit's threads on the fake: the fake's replies, channel info and reactions; the scripted
day (``slack_day``); and the template's poller stepped through that day, a restart before every
poll, relaying each thread's start, replies, check and removal once, with the counts the day says."""

import contextlib
import importlib.util
import io
import json
import time
import urllib.parse
import urllib.request
from collections import Counter

import pytest

from orchestra_bridges.kits.fakes import CANARY, FakeSlack, slack_day
from orchestra_bridges.kits.fakes.slack_day import CHANNELS, policies
from orchestra_bridges.kits.scaffold import scaffold
from orchestra_bridges.kits.testing import set_args


def get(fake, path, **params):
    url = fake.url + path + "?" + urllib.parse.urlencode(params)
    with urllib.request.urlopen(url, timeout=5) as r:
        return json.loads(r.read())


def test_the_fake_answers_replies_info_and_reactions():
    with FakeSlack() as s:
        parent = "1793000000.000100"
        body = get(s, "/api/conversations.replies", channel="C001", ts=parent)
        assert [m["ts"] for m in body["messages"]] == [parent, "1793000060.000200"]     # the parent first
        assert get(s, "/api/conversations.replies", channel="C001", ts="1.0")["error"] == "thread_not_found"
        assert get(s, "/api/conversations.info", channel="C001")["channel"]["name"] == "general"
        s.react("C001", parent, "U002")
        s.react("C001", parent, "U001")
        [m] = [m for m in get(s, "/api/conversations.history", channel="C001")["messages"] if m["ts"] == parent]
        assert m["reactions"] == [{"name": "white_check_mark", "users": ["U002", "U001"], "count": 2}]
        s.unreact("C001", parent, "U002")
        s.unreact("C001", parent, "U001")
        [m] = [m for m in get(s, "/api/conversations.history", channel="C001")["messages"] if m["ts"] == parent]
        assert "reactions" not in m


def test_the_day_holds_each_kind_once_in_each_channel():
    d = slack_day()
    assert len(d.threads) == 30 and Counter(t["kind"] for t in d.threads) == dict.fromkeys(
        ("in_time", "late", "in_time_unchecked", "never_answered", "bump_then_in_time", "checked_then_unchecked"), 5)
    assert d.expected() == {"started": 30, "answered_in_time": 20, "answered_late": 5, "unanswered": 5,
                            "checked": 15, "never_checked": 15, "unchecked": 5}
    for _, name, _ in CHANNELS:
        assert d.expected(name) == {"started": 6, "answered_in_time": 4, "answered_late": 1, "unanswered": 1,
                                    "checked": 3, "never_checked": 3, "unchecked": 1}


def test_play_brings_the_day_up_to_the_time():
    d = slack_day()
    with FakeSlack(d.world()) as s:
        t0 = time.time() - 1000
        s.play(d, t0, auto=False)
        assert s.advance(t0 + 600) == 1                                       # the first thread
        assert get(s, "/api/conversations.history", channel="C0SREHELP")["messages"][0]["user"] == "U101"
        assert s.advance(t0 + d.length_s) == len(d.actions) - 1
        assert CANARY in json.dumps(get(s, "/api/conversations.history", channel="C0ONCALL"))


@pytest.fixture
def program(tmp_path):
    d = scaffold("slack", "slack_threads", tmp_path / "root")
    spec = importlib.util.spec_from_file_location("slack_threads", d / "slack_threads.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_poller_relays_the_day_once_with_its_counts(program, tmp_path, monkeypatch):
    monkeypatch.setenv("ORCHESTRATOR_HOME", str(tmp_path / "home"))
    d = slack_day()
    events_file = tmp_path / "events.jsonl"
    scope = set_args(channels=[c for c, _, _ in CHANNELS], policies=policies(), since="1d")
    with FakeSlack(d.world()) as fake:
        t0 = time.time() - d.length_s - 600
        fake.play(d, t0, auto=False)
        base = ["--base-url", fake.url, "--state-dir", str(tmp_path / "state"), "--ttl", "0", *scope]
        for at in sorted({a["at"] for a in d.actions}):          # a poll (and a restart) after each moment
            fake.advance(t0 + at)
            assert program.main([*base, "--once", "--events-file", str(events_file)]) == 0
        program.main([*base, "--once", "--events-file", str(events_file)])   # once more: nothing new
        events = [json.loads(x) for x in events_file.read_text().splitlines()]
        assert len({e["id"] for e in events}) == len(events)                  # nothing relayed twice
        by = Counter(e["type"] for e in events)
        want = d.expected()
        assert by["slack.thread.started"] == want["started"]
        first = [e for e in events if e["type"] == "slack.thread.replied" and e["data"]["first"]]
        assert len(first) == want["answered_in_time"] + want["answered_late"]
        assert sum(e["data"]["in_time"] for e in first) == want["answered_in_time"]
        assert by["slack.thread.checked"] == want["checked"] + want["unchecked"]
        assert by["slack.thread.unchecked"] == want["unchecked"]
        bumps = [e for e in events if e["type"] == "slack.thread.replied" and e["data"]["by_author"]]
        assert len(bumps) == 4 and not any(e["data"]["first"] for e in bumps)  # an author's bump answers nothing
        started = next(e for e in events if e["type"] == "slack.thread.started" and e["data"]["channel_name"] == "oncall")
        assert started["data"]["policy_s"] == 1800 and started["data"]["first_reply_deadline"]
        assert {e["source"] for e in events if e["type"] == "slack.thread.started" and e["data"]["bot"]} == {"bot.slack.b001"}
        announced = {e["source"]: e["data"]["kind"] for e in events if e["type"] == "orchestra.machine.started"}
        assert announced["bot.slack"] == "program" and announced["person.slack.u201"] == "person"
        assert not any(CANARY in json.dumps(e) for e in events)
        assert {r.path for r in fake.requests} == {"/api/conversations.history", "/api/conversations.replies",
                                                   "/api/conversations.info"}       # no users.info: no users:read
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            assert program.main(["measure", *base]) == 0
        text = buf.getvalue()
        for _, name, _ in CHANNELS:
            assert f'slack_threads_awaiting_reply{{channel="{name}"}} 1' in text
            assert f'slack_threads_unchecked{{channel="{name}"}} 3' in text
