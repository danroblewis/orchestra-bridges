"""The Discord kit on its fake: the fake's shapes (messages newest first with ``after``, threads,
reactions, the guild's channels, the rate-limit headers); the client honouring
``X-RateLimit-Remaining: 0`` and keeping the headers; and the template's poller stepped through the
Slack kit's scripted day on the Discord fake, a restart before every poll, relaying each thread's
start, replies, check and removal once, with the counts the day says, in the Slack kit's shapes."""

import contextlib
import importlib.util
import io
import json
import time
import urllib.error
import urllib.request
from collections import Counter

import pytest

from orchestra_bridges.kits.client import CachedClient
from orchestra_bridges.kits.fakes import CANARY, FakeDiscord, slack_day
from orchestra_bridges.kits.fakes.discord import snowflake, time_of
from orchestra_bridges.kits.fakes.slack_day import CHANNELS, RESPONDERS
from orchestra_bridges.kits.scaffold import scaffold
from orchestra_bridges.kits.testing import set_args

AUTH = "Bot t"


def get(fake, path):
    req = urllib.request.Request(fake.url + "/api/v10" + path, headers={"Authorization": AUTH})
    with urllib.request.urlopen(req, timeout=5) as r:
        return json.loads(r.read()), r.headers


def test_the_fake_answers_in_discords_shapes():
    with FakeDiscord(token="t") as d:
        msgs, headers = get(d, "/channels/C001/messages?limit=10")
        assert headers["X-RateLimit-Remaining"] == "49" and headers["X-RateLimit-Reset-After"]
        assert [m["author"]["id"] for m in msgs] == ["B001", "U001"]               # newest first; no thread reply
        parent = msgs[1]
        assert parent["thread"]["message_count"] == 1 and parent["flags"] == 32
        assert msgs[0]["author"]["bot"] is True
        first, _ = get(d, f"/channels/C001/messages?after={int(parent['id']) - 1}&limit=1")
        assert [m["id"] for m in first] == [parent["id"]]                            # after: the next ones
        thread, _ = get(d, f"/channels/{parent['id']}/messages")
        assert [m["type"] for m in reversed(thread)] == [21, 0]                      # its starter first
        d.react("C001", parent["id"], "U002")
        msgs, _ = get(d, "/channels/C001/messages")
        assert msgs[1]["reactions"][0]["emoji"] == {"id": None, "name": "✅"} and msgs[1]["reactions"][0]["count"] == 1
        users, _ = get(d, f"/channels/C001/messages/{parent['id']}/reactions/%E2%9C%85")
        assert [u["id"] for u in users] == ["U002"]
        reply = d.post("C001", "U002", reply_to=parent["id"])
        [m], _ = get(d, f"/channels/C001/messages?after={int(reply) - 1}")
        assert m["type"] == 19 and m["message_reference"]["message_id"] == parent["id"]
        active, _ = get(d, "/guilds/1000/threads/active")
        assert [t["id"] for t in active["threads"]] == [parent["id"]]
        d.archive(parent["id"])
        archived, _ = get(d, "/channels/C001/threads/archived/public")
        assert [t["id"] for t in archived["threads"]] == [parent["id"]]
        assert not get(d, "/guilds/1000/threads/active")[0]["threads"]
        chans, _ = get(d, "/guilds/1000/channels")
        assert {(c["id"], c["type"]) for c in chans} == {("C001", 0), ("C900", 4), ("C901", 2)}
        assert CANARY in json.dumps(get(d, "/channels/C001/messages")[0])
        with pytest.raises(urllib.error.HTTPError) as e:
            get(d, "/channels/C404/messages")
        assert e.value.code == 404


def test_snowflakes_are_times():
    t = 1793000000.0
    assert time_of(snowflake(t)) == pytest.approx(t)


def test_the_client_waits_out_an_empty_bucket_and_keeps_the_headers(tmp_path):
    slept = []
    with FakeDiscord(token="t") as d:
        d.rate = {**d.rate, "X-RateLimit-Remaining": "0", "X-RateLimit-Reset-After": "2.5"}
        now = [1000.0]
        c = CachedClient(d.url, token="t", auth_format="Bot {token}", cache_dir=tmp_path / "c", rate_dir=tmp_path / "r",
                         clock=lambda: now[0], sleep=lambda s: (slept.append(s), now.__setitem__(0, now[0] + s)))
        c.get("/api/v10/channels/C001")
        assert c.rate_headers["x-ratelimit-remaining"] == "0" and c.rate_headers["x-ratelimit-bucket"] == "fakebucket"
        assert not slept
        c.get("/api/v10/channels/C001")                       # the bucket was empty: wait for its reset
        assert sum(slept) == pytest.approx(2.5, abs=0.1)


@pytest.fixture
def program(tmp_path):
    d = scaffold("discord", "discord_threads", tmp_path / "root")
    spec = importlib.util.spec_from_file_location("discord_threads", d / "discord_threads.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_poller_relays_the_day_once_with_its_counts(program, tmp_path, monkeypatch):
    """The Slack kit's day on the Discord fake: the same counts, in the same shapes."""
    monkeypatch.setenv("ORCHESTRATOR_HOME", str(tmp_path / "home"))
    d = slack_day()
    events_file = tmp_path / "events.jsonl"
    scope = set_args(channels=[c for c, _, _ in CHANNELS], guild="1000", since="1d")
    with FakeDiscord(d.world()) as fake:
        t0 = time.time() - d.length_s - 600
        fake.play(d, t0, auto=False)
        base = ["--base-url", fake.url, "--state-dir", str(tmp_path / "state"), "--ttl", "0", *scope]
        for at in sorted({a["at"] for a in d.actions}):          # a poll (and a restart) after each moment
            fake.advance(t0 + at)
            assert program.main([*base, "--once", "--events-file", str(events_file)]) == 0
        program.main([*base, "--once", "--events-file", str(events_file)])   # once more: nothing new
        events = [json.loads(x) for x in events_file.read_text().splitlines()]
        assert len({e["id"] for e in events}) == len(events)
        by = Counter(e["type"] for e in events)
        want = d.expected()
        assert by["discord.thread.started"] == want["started"]
        first = [e for e in events if e["type"] == "discord.thread.replied" and e["data"]["first"]]
        assert len(first) == want["answered_in_time"] + want["answered_late"]
        assert by["discord.thread.checked"] == want["checked"] + want["unchecked"]
        assert by["discord.thread.unchecked"] == want["unchecked"]
        for e in (e for e in events if e["type"] == "discord.thread.unchecked"):
            assert e["source"] == f"person.discord.{e['data']['by'].lower()}" and e["data"]["by"] in RESPONDERS
        assert all(e["data"]["emoji"] == "white_check_mark"
                   for e in events if e["type"] in ("discord.thread.checked", "discord.thread.unchecked"))
        assert all(e["data"]["permalink"].startswith("https://discord.com/channels/1000/")
                   for e in events if e["type"].startswith("discord.thread."))
        bumps = [e for e in events if e["type"] == "discord.thread.replied" and e["data"]["by_author"]]
        assert len(bumps) == 4 and not any(e["data"]["first"] for e in bumps)
        assert {e["source"] for e in events if e["type"] == "discord.thread.started" and e["data"]["bot"]} == {
            "bot.discord.b001"}
        assert not any(CANARY in json.dumps(e) for e in events)
        assert {r.method for r in fake.requests} == {"GET"}
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            assert program.main(["measure", *base]) == 0
        text = buf.getvalue()
        for _, name, _ in CHANNELS:
            assert f'discord_threads_awaiting_reply{{channel="{name}"}} 1' in text
            assert f'discord_threads_unchecked{{channel="{name}"}} 3' in text


def slack_shape(e: dict) -> tuple:
    return e["type"].replace("discord.", "slack.", 1), tuple(sorted((e.get("data") or {}).keys()))


def test_the_events_have_the_slack_kits_shapes(tmp_path, monkeypatch):
    """The same day through both kits: the same types (but the prefix) and the same data keys."""
    from orchestra_bridges.kits.fakes import FakeSlack
    monkeypatch.setenv("ORCHESTRATOR_HOME", str(tmp_path / "home"))
    d = slack_day(6)
    shapes = {}
    for kind, Fake in (("slack", FakeSlack), ("discord", FakeDiscord)):
        root = tmp_path / kind
        m = scaffold(kind, f"{kind}_threads", root)
        spec = importlib.util.spec_from_file_location(kind, m / f"{kind}_threads.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        with Fake(d.world()) as fake:
            t0 = time.time() - d.length_s - 600
            fake.play(d, t0, auto=False)
            ev = root / "events.jsonl"
            base = ["--base-url", fake.url, "--state-dir", str(root / "state"), "--ttl", "0", "--events-file", str(ev),
                    *set_args(channels=[c for c, _, _ in CHANNELS], since="1d")]
            for at in sorted({a["at"] for a in d.actions}):
                fake.advance(t0 + at)
                assert mod.main([*base, "--once"]) == 0
        events = [json.loads(x) for x in ev.read_text().splitlines() if '"orchestra.machine.started"' not in x]
        shapes[kind] = Counter(slack_shape(e) if kind == "discord" else (e["type"], tuple(sorted(e["data"])))
                               for e in events)
    assert shapes["discord"] == shapes["slack"]
