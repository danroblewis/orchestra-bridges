"""discord_example against the kit's Discord fake: two polls (a restart between them) relay each thread and
message once, as who acted; a reply in the thread and an inline one; a check and its removal;
``measure`` prints the channel's counts; ``channels`` lists the guild's; no text or token leaves.

Run with Orchestra's own Python (the one ``orchestra`` runs in, which has the kits): ``python -m pytest .orchestra/machines/discord_example/test_discord_example.py``."""

import importlib.util
import json
import time
from pathlib import Path

import pytest

from orchestra_bridges.kits.fakes import CANARY, FakeDiscord
from orchestra_bridges.kits.fakes.base import load_fixture
from orchestra_bridges.kits.testing import fake_token, set_args, settings, token_env

HERE = Path(__file__).resolve().parent
TOKEN = fake_token(HERE)        # the placeholder the runner sends a fake on this machine
REAL = "real-token-never-sent-to-a-fake"   # in the environment, yet never read for a fake
TOKEN_ENV = token_env(HERE, "DISCORD_TOKEN")         # poller.toml's, so the owner's variable works too
CHANNEL = str((settings(HERE).get("channels") or ["C001"])[0])   # the fake holds poller.toml's first channel
GUILD = str(settings(HERE).get("guild") or "1000")
SCOPE = set_args(channels=[CHANNEL], guild=GUILD, since="1d", cadence={}, max_backoff="1h")


def program():
    spec = importlib.util.spec_from_file_location("discord_example", HERE / "discord_example.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def recent_world() -> dict:
    """The kit's fixture, its messages moved to the last minutes (the poller reads back a day)."""
    world, now = load_fixture(None, "discord.json"), time.time()
    msgs = world["channels"].pop("C001")
    world["channels"][CHANNEL] = msgs
    world["names"] = {CHANNEL: "general"}
    world["guild"]["id"] = GUILD
    for n, m in enumerate(msgs):
        m["at"] = now - 600 + 60 * n
    return world


@pytest.fixture
def fake(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCHESTRATOR_HOME", str(tmp_path / "home"))
    monkeypatch.setenv(TOKEN_ENV, REAL)
    with FakeDiscord(recent_world(), token=TOKEN) as f:
        yield f


def args(fake, tmp, *extra, scope=SCOPE):
    return [*extra, "--base-url", fake.url, "--state-dir", str(tmp / "state"), "--ttl", "0", *scope]


def poll_once(fake, tmp, scope=SCOPE):
    assert program().main(args(fake, tmp, "--once", "--events-file", str(tmp / "events.jsonl"), scope=scope)) == 0
    return [json.loads(x) for x in (tmp / "events.jsonl").read_text().splitlines()]


def posted(events):
    return [(e["source"], e["data"]["reply"]) for e in events if e["type"] == "discord.message.posted"]


def threads(events):
    return [(e["type"].rsplit(".", 1)[1], e["source"]) for e in events if e["type"].startswith("discord.thread.")]


def test_two_polls_relay_each_thread_and_message_once(fake, tmp_path):
    events = poll_once(fake, tmp_path)
    assert posted(events) == [("person.discord.u001", False), ("person.discord.u002", True),
                              ("bot.discord.b001", False)]
    assert threads(events) == [("started", "person.discord.u001"), ("replied", "person.discord.u002"),
                               ("started", "bot.discord.b001")]
    replied = next(e for e in events if e["type"] == "discord.thread.replied")
    assert replied["data"]["first"] and not replied["data"]["by_author"] and replied["data"]["channel_name"] == "general"
    parent = replied["subject"].split("/")[1]
    fake.post(CHANNEL, "U002")
    fake.post(CHANNEL, "U001", reply_to=parent)                              # an inline reply: the author's bump
    fake.react(CHANNEL, parent, "U002")
    events = poll_once(fake, tmp_path)
    assert posted(events)[3:] == [("person.discord.u002", False), ("person.discord.u001", True)]
    assert threads(events)[3:] == [("checked", "person.discord.u002"), ("started", "person.discord.u002"),
                                   ("replied", "person.discord.u001")]
    bump = [e for e in events if e["type"] == "discord.thread.replied"][-1]
    assert bump["data"]["by_author"] and not bump["data"]["first"]
    checked = next(e for e in events if e["type"] == "discord.thread.checked")
    assert checked["data"]["emoji"] == "white_check_mark"                    # Slack's name: one workflow setting
    fake.unreact(CHANNEL, parent, "U002")
    events = poll_once(fake, tmp_path)
    assert threads(events)[6:] == [("unchecked", "person.discord.u002")]     # only who checked it can remove it
    unchecked = next(e for e in events if e["type"] == "discord.thread.unchecked")
    assert unchecked["data"]["by"] == "U002"
    assert unchecked["data"]["permalink"] == f"https://discord.com/channels/{GUILD}/{CHANNEL}/{parent}"
    fake.react(CHANNEL, parent, "U001")
    fake.react(CHANNEL, parent, "U002")
    poll_once(fake, tmp_path)
    fake.unreact(CHANNEL, parent, "U001")
    fake.unreact(CHANNEL, parent, "U002")
    events = poll_once(fake, tmp_path)                                         # both removed: whose was last?
    assert threads(events)[-1] == ("unchecked", "person.discord.u001")       # the thread's author
    assert events[-1]["data"]["by"] == "unknown"
    assert not any(e["source"] == "bot.discord" for e in events)             # never a source of its own
    assert all(r.authorized and r.method == "GET" for r in fake.requests)


def test_an_archived_threads_reply_is_still_relayed(fake, tmp_path):
    events = poll_once(fake, tmp_path)
    parent = next(e for e in events if e["type"] == "discord.thread.replied")["subject"].split("/")[1]
    fake.reply(CHANNEL, parent, "U002")
    fake.archive(parent)
    events = poll_once(fake, tmp_path)
    assert threads(events)[-1] == ("replied", "person.discord.u002")
    assert fake.requests_to(f"/api/v10/channels/{CHANNEL}/threads/archived/public")


def read_of(fake, channel):
    return fake.requests_to(f"/api/v10/channels/{channel}/messages")


def test_a_channel_on_its_own_cadence_is_read_only_when_due(fake, tmp_path):
    fake.data["channels"]["C0QUIET"], fake.data["names"]["C0QUIET"] = [], "quiet"
    scope = set_args(channels=[CHANNEL, "C0QUIET"], guild=GUILD, since="1d", cadence={"C0QUIET": "1h"},
                     max_backoff="1h")
    poll_once(fake, tmp_path, scope)
    poll_once(fake, tmp_path, scope)                       # a restart, at once: the quiet one is not due
    assert len(read_of(fake, CHANNEL)) == 2 and len(read_of(fake, "C0QUIET")) == 1


def test_a_rate_limited_channel_backs_off_and_the_others_go_on(fake, tmp_path, capsys):
    fake.data["channels"]["C0BUSY"], fake.data["names"]["C0BUSY"] = [], "busy"
    scope = set_args(channels=["C0BUSY", CHANNEL], guild=GUILD, since="1d", cadence={}, max_backoff="1h")
    fake.fail_next(429, {"Retry-After": "3600", "X-RateLimit-Scope": "user"})   # longer than the client waits
    events = poll_once(fake, tmp_path, scope)               # the poll itself succeeds
    assert ("started", "person.discord.u001") in threads(events)          # the other channel was read
    assert "C0BUSY rate limited (429)" in capsys.readouterr().err
    cursor = json.loads(next((tmp_path / "state").rglob("cursor.json")).read_text())
    assert cursor["cursor"]["C0BUSY"]["backoff"] >= 1 and cursor["cursor"]["C0BUSY"]["due"] > time.time()
    before = len(fake.requests)
    poll_once(fake, tmp_path, scope)                        # backing off: the busy channel is not read
    assert not [r for r in fake.requests[before:] if "/C0BUSY" in r.path]


def test_measure_counts_the_channel(fake, tmp_path, capsys):
    assert program().main(args(fake, tmp_path, "measure")) == 0
    out = capsys.readouterr().out
    assert 'discord_channel_messages_last_hour{channel="general"} 2' in out
    assert 'discord_threads_awaiting_reply{channel="general"} 1' in out     # the bot's, unanswered
    assert 'discord_threads_unchecked{channel="general"} 2' in out


def test_channels_lists_the_guilds_by_id_type_and_name(fake, tmp_path, capsys):
    assert program().main(args(fake, tmp_path, "channels")) == 0
    lines = capsys.readouterr().out.splitlines()
    assert lines[0] == f"{CHANNEL}\ttext\t#general" and "C901\tvoice\t#voice" in lines


def test_no_text_or_token_leaves(fake, tmp_path, capsys):
    poll_once(fake, tmp_path)
    program().main(args(fake, tmp_path, "measure"))
    program().main(args(fake, tmp_path, "channels"))
    out = capsys.readouterr()
    assert CANARY not in out.out + out.err and REAL not in out.out + out.err
    for p in tmp_path.rglob("*"):
        if p.is_file():
            text = p.read_text(errors="replace")
            assert CANARY not in text and REAL not in text, p
