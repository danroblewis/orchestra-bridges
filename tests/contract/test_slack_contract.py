"""The fake Slack against the real Web API, and the canary against the real API (opt in:
``ORCHESTRA_BRIDGES_SLACK_CONTRACT=1``, with ``SLACK_USER_TOKEN`` or the token file set up as the
README says). Read-only: ``auth.test``, a few ``search.messages`` and ``reactions.list`` calls.

Only shapes are compared, never content: every key the fake serves must be one the real API serves
(the fake invents nothing), and the fields the bridge reads must have the same types. Slack adds
keys from time to time; a real key the fake lacks is printed, not failed.

**The canary.** Before running, have someone else (a second account in a personal workspace is
enough) send the token's owner a direct message and a mention whose text includes the value of
``SLACK_CANARY``, e.g. ``SLACK_CANARY=CANARY-$(uuidgen)``, within the last 48 hours. With
``SLACK_CANARY`` set, the default searches, spelled as the bridge spells them (with its ``after:``),
must each find a ping of their kind; ``SLACK_GROUP`` (a user group id the owner is in, mentioned in
such a message) adds the group search. The test then runs the bridge once against the real API into
a scratch project, checks that a DM and a mention were published, and that the canary is in no
event, file, log line or metric."""

import json
import os
import threading
import time
from pathlib import Path

import pytest
from fake_slack import CHANNEL, OWNER, FakeSlack

from orchestra_bridges.secret import read_token
from orchestra_bridges.slack import search
from orchestra_bridges.slack.api import Slack

pytestmark = pytest.mark.skipif(os.environ.get("ORCHESTRA_BRIDGES_SLACK_CONTRACT") != "1",
                                reason="set ORCHESTRA_BRIDGES_SLACK_CONTRACT=1 to call the real Slack API")
READ = {"match": {"ts": str, "channel": dict, "permalink": str, "text": str},
        "channel": {"id": str, "is_im": bool, "is_mpim": bool, "is_private": bool},
        "paging": {"pages": int, "page": int},
        "auth": {"user_id": str, "team_id": str, "user": str},
        "reactions.list": {"items": list, "response_metadata": dict},
        "item": {"type": str, "channel": str, "message": dict},
        "reacted message": {"ts": str, "reactions": list},
        "reaction": {"name": str, "users": list, "count": int}}
canary = pytest.mark.skipif(not os.environ.get("SLACK_CANARY"), reason="set SLACK_CANARY to a string a ping to you contains")


def api() -> Slack:
    return Slack(read_token("SLACK_USER_TOKEN", "slack-token", Path.cwd()))


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


@canary
@pytest.mark.parametrize("which", ["mention", "dm", "group"])
def test_each_default_search_finds_a_ping_of_its_kind(me, which):
    """Each search as the bridge sends it (with ``after:``, from ``search.since``) finds a recent ping
    that ``kind`` takes for its own: a search that finds none is a spelling Slack does not read
    (adjust ``queries``)."""
    from orchestra_bridges.slack.pings import kind
    group = os.environ.get("SLACK_GROUP")
    if which == "group" and not group:
        pytest.skip("set SLACK_GROUP to a user group id mentioned in a recent message")
    who = me["user_id"]
    q = {"mention": search.mention_query(who), "dm": search.dm_query(who), "group": search.group_query(group or "")}[which]
    got = search.since(api(), q, time.time() - 48 * 3600, {who, *([group] if group else [])})
    kinds = {kind(m, who, [group] if group else [], True) for m in got}
    assert which in kinds, f"{q!r} with after: found {len(got)} messages, none a {which}"


def test_reactions_list_shape(world, me):
    body = api().call("reactions.list", {"full": "true", "limit": 20})
    real = [i for i in body.get("items") or [] if i.get("type") == "message"]
    if not real:
        pytest.skip("the owner has reacted to no message")
    fake = world.reactions_list({})
    compare("reactions.list", [fake], [body])
    compare("response_metadata", [fake["response_metadata"]], [body["response_metadata"]])
    compare("item", fake["items"], real)
    compare("reacted message", [i["message"] for i in fake["items"]], [i["message"] for i in real])
    compare("reaction", [r for i in fake["items"] for r in i["message"]["reactions"]],
            [r for i in real for r in i["message"].get("reactions") or []])
    assert all(isinstance(u, str) for i in real for r in i["message"].get("reactions") or [] for u in r["users"])


@canary
def test_the_canary_never_leaves_the_bridge(tmp_path, monkeypatch, capsys):
    from conftest import new_project
    from orchestra.events import Events
    from orchestra.metrics import ProjectStore, for_project

    from orchestra_bridges import config as cfg
    from orchestra_bridges.slack.bridge import Bridge
    canary = os.environ["SLACK_CANARY"]
    project = new_project(tmp_path, monkeypatch, '[bridges.slack]\nlookback = "48h"\nport = 0\nanswered = ["reaction", "reply"]\n')
    conf = cfg.slack(project.config.raw, project.repo_root)
    b = Bridge(conf, api(), Events.for_project(project), ProjectStore(for_project(project), project.key))
    b.run(threading.Event(), once=True)
    evs = Events.for_project(project).after(0)[0]
    kinds = {e["data"]["kind"] for e in evs if e["type"] == "slack.ping.received"}
    assert {"dm", "mention"} <= kinds, f"found pings of kinds {sorted(kinds)}: have someone send you a DM and a mention"
    out = capsys.readouterr()
    for where in (json.dumps(evs), b.render() or "", out.out + out.err):
        assert canary not in where
    for f in tmp_path.rglob("*"):
        if f.is_file() and ".git" not in f.parts:
            assert canary.encode() not in f.read_bytes(), f
