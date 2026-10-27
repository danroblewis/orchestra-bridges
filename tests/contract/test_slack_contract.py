"""The fake Slack against the real Web API, and the canary against the real API (opt in:
``ORCHESTRA_BRIDGES_SLACK_CONTRACT=1``, with ``SLACK_USER_TOKEN`` or the token file set up as the
README says). Read-only: ``auth.test``, a few ``search.messages`` and ``reactions.get`` calls.

Only shapes are compared, never content: every key the fake serves must be one the real API serves
(the fake invents nothing), and the fields the bridge reads must have the same types. Slack adds
keys from time to time; a real key the fake lacks is printed, not failed.

**The canary.** Before running, have someone else (a second account in a personal workspace is
enough) send the token's owner a direct message and a mention whose text includes the value of
``SLACK_CANARY``, e.g. ``SLACK_CANARY=CANARY-$(uuidgen)``. The test runs the bridge once against the
real API into a scratch project, checks that those pings were published, and that the canary is in
no event, file, log line or metric."""

import json
import os
import threading
from pathlib import Path

import pytest
from fake_slack import CHANNEL, OWNER, FakeSlack

from orchestra_bridges.slack import search
from orchestra_bridges.slack.api import Slack
from orchestra_bridges.slack.token import user_token

pytestmark = pytest.mark.skipif(os.environ.get("ORCHESTRA_BRIDGES_SLACK_CONTRACT") != "1",
                                reason="set ORCHESTRA_BRIDGES_SLACK_CONTRACT=1 to call the real Slack API")
READ = {"match": {"ts": str, "channel": dict, "permalink": str, "text": str},
        "channel": {"id": str, "is_im": bool, "is_mpim": bool, "is_private": bool},
        "paging": {"pages": int, "page": int},
        "auth": {"user_id": str, "team_id": str, "user": str}}


def api() -> Slack:
    return Slack(user_token(Path.cwd()))


def keys(items):
    return set().union(*(set(i) for i in items))


def compare(name, fake_items, real_items):
    assert real_items, f"the real API returned no {name}"
    invented = keys(fake_items) - keys(real_items)
    assert not invented, f"the fake's {name} has keys the real API's has not: {sorted(invented)}"
    if missing := keys(real_items) - keys(fake_items):
        print(f"{name}: real keys the fake does not serve: {sorted(missing)}")
    for field, typ in READ.get(name, {}).items():
        assert all(isinstance(i[field], typ) for i in real_items if i.get(field) is not None), (name, field)


@pytest.fixture(scope="module")
def world():
    s = FakeSlack(clock=lambda: 2e9)
    s.user("U0BOB", "bob")
    root = s.say("U0BOB", CHANNEL, 1000.0, mentions=(OWNER,))
    s.say("U0BOB", s.dm("U0BOB"), 1001.0)
    s.say(OWNER, CHANNEL, 1002.0, thread=root)
    s.react(CHANNEL, root, "white_check_mark", OWNER, 1003.0)
    return s


@pytest.fixture(scope="module")
def me():
    return api().auth()


def test_auth(me):
    fake = {"ok": True, "url": "", "team": "", "user": "", "team_id": "", "user_id": "", "is_enterprise_install": False}
    compare("auth", [fake], [me])


def test_search_shapes(world, me):
    real = api().search(f"<@{me['user_id']}>", 1, 20)
    fake = world.search({"query": "", "count": "20"})["messages"]
    compare("messages", [fake], [real])
    compare("paging", [fake["paging"]], [real["paging"]])
    compare("match", fake["matches"], real["matches"])
    compare("channel", [m["channel"] for m in fake["matches"]], [m["channel"] for m in real["matches"]])
    for m in real["matches"]:
        search.message(m, {me["user_id"]})


def test_each_default_search_finds_what_it_should(me):
    """Prints how many of each search's matches the bridge would take as pings: a search that finds
    none of its kind is a spelling Slack does not read (adjust ``queries``)."""
    from orchestra_bridges.slack.pings import kind
    a = api()
    for q in (search.mention_query(me["user_id"]), search.dm_query(me["user_id"]), search.own_query(me["user_id"])):
        got = [search.message(m, {me["user_id"]}) for m in a.search(q, 1, 50).get("matches") or []]
        kinds = [kind(m, me["user_id"], [], True) for m in got if m]
        print(f"{q!r}: {len(got)} matches; as pings: {', '.join(sorted({str(k) for k in kinds}))}")


def test_reactions_shape(world, me):
    a = api()
    [m] = a.search(f"from:<@{me['user_id']}>", 1, 1).get("matches") or [None]
    if m is None:
        pytest.skip("the owner has no messages to read reactions on")
    body = a.call("reactions.get", {"channel": m["channel"]["id"], "timestamp": m["ts"], "full": "true"})
    fake = world.reactions_get({"channel": CHANNEL, "timestamp": world.messages[0]["ts"]})
    compare("reactions.get", [fake], [body])


@pytest.mark.skipif(not os.environ.get("SLACK_CANARY"), reason="set SLACK_CANARY to a string a ping to you contains")
def test_the_canary_never_leaves_the_bridge(tmp_path, monkeypatch, capsys):
    from conftest import new_project
    from orchestra.events import Events
    from orchestra.metrics import ProjectStore, for_project

    from orchestra_bridges import config as cfg
    from orchestra_bridges.slack.bridge import MACHINE, Bridge
    canary = os.environ["SLACK_CANARY"]
    project = new_project(tmp_path, monkeypatch, '[bridges.slack]\nlookback = "48h"\nport = 0\nanswered = ["reaction", "reply"]\n')
    conf = cfg.slack(project.config.raw, project.repo_root)
    b = Bridge(conf, api(), Events.for_project(project, source=MACHINE), ProjectStore(for_project(project), project.key))
    b.run(threading.Event(), once=True)
    evs = Events.for_project(project).after(0)[0]
    assert [e for e in evs if e["type"] == "slack.ping.received"], "no pings found: send yourself one with the canary"
    out = capsys.readouterr()
    for where in (json.dumps(evs), b.render() or "", out.out + out.err):
        assert canary not in where
    for f in tmp_path.rglob("*"):
        if f.is_file() and ".git" not in f.parts:
            assert canary.encode() not in f.read_bytes(), f
