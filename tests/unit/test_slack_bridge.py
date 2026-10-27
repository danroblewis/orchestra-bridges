"""The Slack bridge against the fake Slack: pings, the checkmark and reply rules, the stock and its
counters as a scrape stores them, restarts, gaps, rate limits, and no text anywhere."""

import json
import threading
import urllib.error
import urllib.request
from itertools import pairwise

import pytest
from conftest import T0, run_once
from fake_slack import CANARY, CHANNEL, GROUP, OTHER, OWNER, TEAM, TOKEN
from orchestra.collect.round import points_of
from orchestra.collect.spec import Collector
from orchestra.events import Events
from orchestra.metrics import for_project

from orchestra_bridges.slack.waits import COUNTERS

BOB, CAROL = "U0BOB", "U0CAROL"
PEOPLE = ('[people.daniel]\nname = "Daniel Lewis"\nteam = "sre"\nslack = "U0OWNER"\n'
          '[people.bob]\nname = "Bob Builder"\nteam = "platform"\nslack = "bob"\n')


def bus(project) -> list[dict]:
    return Events.for_project(project).after(0)[0]


def published(project, type_=None) -> list[dict]:
    return [e for e in bus(project) if e["type"] != "orchestra.machine.started" and (type_ is None or e["type"] == type_)]


def scrape(bridge, project, at: float) -> None:
    """What Orchestra's [[scrape]] named bridges.slack does with the bridge's /metrics at ``at``."""
    text = bridge.render()
    assert text is not None
    r = points_of(Collector("bridges.slack", "scrape", "http://127.0.0.1/metrics"), text, project.key, at)
    for_project(project).write(r.points)


def stored(project, series, **labels):
    last = for_project(project).latest(series, {"machine": "bridges.slack", "workspace": TEAM, **labels})
    return last[1] if last else None


def served(bridge) -> dict[tuple, float]:
    out = {}
    for line in bridge.render().splitlines():
        if line and not line.startswith("#"):
            name, value = line.rsplit(" ", 1)
            out[name] = float(value)
    return out


def level(bridge, name, **labels) -> float:
    labels = {"workspace": TEAM, **labels}
    return served(bridge)[name + "{" + ",".join(f'{k}="{v}"' for k, v in labels.items()) + "}"]


def tick(bridge, clock, project=None, seconds=60.0, times=1):
    for _ in range(times):
        clock.t += seconds
        bridge.round()
        if project is not None:
            scrape(bridge, project, clock.t)


@pytest.fixture
def people(slack_project):
    (slack_project.repo_root / "people.toml").write_text(PEOPLE)


def test_a_mention_is_a_ping_from_the_pinger_on_the_thread(slack_project, slack, clock, make_slack, people):
    slack.user(BOB, "bob")
    ts = slack.say(BOB, CHANNEL, T0 - 120, mentions=(OWNER,))
    slack.say(BOB, CHANNEL, T0 - 100)                                  # no mention: not a ping
    slack.say(OWNER, CHANNEL, T0 - 90, mentions=(OWNER,))              # the owner's own: not a ping
    b = make_slack()
    run_once(b)
    [p] = published(slack_project)
    assert (p["type"], p["source"], p["subject"]) == ("slack.ping.received", "person.bob", f"{TEAM}/{CHANNEL}/{ts}")
    assert p["id"] == f"slack:{TEAM}:ping:{CHANNEL}:{ts}"
    assert p["time"] == "2026-10-26T07:31:20.000001+00:00"             # Slack's time, to the microsecond
    assert p["data"] == {"workspace": TEAM, "channel": CHANNEL, "channel_type": "channel", "ts": ts, "thread_ts": None,
                         "user": BOB, "kind": "mention"}
    [a] = [e for e in bus(slack_project) if e["source"] == "person.bob"
           and e["type"] == "orchestra.machine.started"]
    assert (a["data"]["name"], a["data"]["team"], a["data"]["kind"], a["data"]["slack"]) == (
        "Bob Builder", "platform", "person", "bob")
    assert level(b, "slack_pings_unanswered") == 1 and level(b, "slack_pings_oldest_unanswered_seconds") == 120


def test_dms_group_mentions_and_replies_in_threads_are_pings(slack_project, slack, clock, make_slack, slack_toml):
    slack.user(BOB, "bob")
    slack.user(CAROL, "carol")
    dm = slack.dm(BOB)
    root = slack.say(CAROL, OTHER, T0 - 600)
    slack.say(BOB, dm, T0 - 300)
    slack.say(CAROL, OTHER, T0 - 200, mentions=(OWNER,), thread=root)
    slack.say(CAROL, CHANNEL, T0 - 100, mentions=(GROUP,))             # no group configured: not a ping
    run_once(make_slack())
    got = [(e["source"], e["data"]["kind"], e["subject"]) for e in published(slack_project)]
    assert got == [("person.bob", "dm", f"{TEAM}/{dm}/{slack.messages[1]['ts']}"),
                   ("person.carol", "mention", f"{TEAM}/{OTHER}/{root}")]


@pytest.mark.parametrize("slack_toml", ['groups = ["S0SRE"]\ndms = false'])
def test_configured_groups_count_and_dms_can_be_left_out(slack_project, slack, clock, make_slack):
    slack.user(CAROL, "carol")
    slack.say(CAROL, slack.dm(CAROL), T0 - 300)
    slack.say(CAROL, CHANNEL, T0 - 100, mentions=(GROUP,))
    run_once(make_slack())
    assert [e["data"]["kind"] for e in published(slack_project)] == ["group"]
    assert not [r for r in slack.requests if r[0] == "search.messages" and r[1]["query"].startswith("to:")]


def test_a_checkmark_by_the_owner_answers_and_moves_the_stock_and_the_within_counters(
        slack_project, slack, clock, make_slack, people):
    slack.user(BOB, "bob")
    ts = slack.say(BOB, CHANNEL, T0 - 30, mentions=(OWNER,))
    slack.react(CHANNEL, ts, "eyes", OWNER, T0 + 60)                   # not a checkmark
    slack.react(CHANNEL, ts, "white_check_mark", BOB, T0 + 60)         # not the owner
    slack.react(CHANNEL, ts, "white_check_mark", OWNER, T0 + 150)
    b = make_slack()
    run_once(b)
    scrape(b, slack_project, clock.t)
    assert stored(slack_project, "slack_pings_unanswered") == 1
    tick(b, clock, slack_project)                                      # T0+60: nothing that answers yet
    assert published(slack_project, "slack.ping.answered") == []
    tick(b, clock, slack_project, times=2)                             # T0+180: seen
    [a] = published(slack_project, "slack.ping.answered")
    assert (a["source"], a["subject"], a["id"]) == ("person.daniel", f"{TEAM}/{CHANNEL}/{ts}", f"slack:{TEAM}:answered:{CHANNEL}:{ts}")
    assert a["time"] == "2026-10-26T07:36:20+00:00"                    # when the bridge saw it: Slack keeps no reaction time
    assert a["data"] == {"workspace": TEAM, "channel": CHANNEL, "ts": ts, "wait_seconds": 210.0, "rule": "reaction",
                         "reaction": "white_check_mark", "observed": True, "not_before": "2026-10-26T07:35:20+00:00"}
    assert stored(slack_project, "slack_pings_unanswered") == 0
    assert stored(slack_project, "slack_pings_received_total") == 1
    assert stored(slack_project, "slack_pings_answered_total", within="5m") == 1
    assert stored(slack_project, "slack_pings_answered_total", within="15m") == 0
    assert stored(slack_project, "slack_ping_wait_seconds_total") == 210.0
    owner = [e for e in bus(slack_project) if e["source"] == "person.daniel" and e["type"] == "orchestra.machine.started"]
    assert [(e["data"]["name"], e["data"]["team"]) for e in owner] == [("Daniel Lewis", "sre")]


def test_a_checkmark_answers_the_earlier_pings_in_its_thread(slack_project, slack, clock, make_slack):
    slack.user(BOB, "bob")
    first = slack.say(BOB, CHANNEL, T0 - 50, mentions=(OWNER,))
    second = slack.say(BOB, CHANNEL, T0 - 40, mentions=(OWNER,), thread=first)
    slack.react(CHANNEL, second, "heavy_check_mark", OWNER, T0 + 10)
    b = make_slack()
    run_once(b)
    tick(b, clock)
    assert sorted(e["data"]["ts"] for e in published(slack_project, "slack.ping.answered")) == [first, second]
    assert level(b, "slack_pings_unanswered") == 0
    slack.requests.clear()
    tick(b, clock)                                                     # answered pings are not asked about again
    assert [r[0] for r in slack.requests if r[0] == "reactions.get"] == []


@pytest.mark.parametrize("slack_toml", ['answered = ["reply"]'])
def test_the_reply_rule_answers_at_the_owners_first_message_after_the_ping(slack_project, slack, clock, make_slack):
    slack.user(BOB, "bob")
    slack.user(CAROL, "carol")
    dm = slack.dm(BOB)
    other = slack.say(CAROL, CHANNEL, T0 - 3100)
    root = slack.say(BOB, CHANNEL, T0 - 3000, mentions=(OWNER,))
    slack.say(OWNER, CHANNEL, T0 - 2990, thread=other)                   # another thread: not an answer
    slack.say(OWNER, CHANNEL, T0 - 2000, thread=root)                    # 1000 s later in its thread: 60m
    asked = slack.say(BOB, dm, T0 - 400)
    slack.say(OWNER, dm, T0 - 110)                                       # 290 s later in the DM: 5m
    slack.say(CAROL, OTHER, T0 - 50, mentions=(OWNER,))                  # still waiting
    slack.react(CHANNEL, root, "white_check_mark", OWNER, T0 - 2500)     # reactions do not count under this rule
    b = make_slack()
    run_once(b)
    answers = {e["data"]["ts"]: e for e in published(slack_project, "slack.ping.answered")}
    assert set(answers) == {root, asked}
    assert answers[root]["data"]["rule"] == "reply" and answers[root]["data"]["wait_seconds"] == 1000.0
    assert answers[asked]["time"] == "2026-10-26T07:31:30.000006+00:00"   # the reply's own time
    assert (level(b, "slack_pings_answered_total", within="5m"), level(b, "slack_pings_answered_total", within="60m"),
            level(b, "slack_pings_unanswered")) == (1, 1, 1)
    assert not [r for r in slack.requests if r[0] == "reactions.get"]


@pytest.mark.parametrize("slack_toml", ['answered = ["reaction", "reply"]'])
def test_with_both_rules_the_earlier_answer_wins(slack_project, slack, clock, make_slack):
    slack.user(BOB, "bob")
    ts = slack.say(BOB, CHANNEL, T0 - 30, mentions=(OWNER,))
    slack.react(CHANNEL, ts, "white_check_mark", OWNER, T0 + 30)
    slack.say(OWNER, CHANNEL, T0 + 40, thread=ts)
    b = make_slack()
    run_once(b)
    tick(b, clock)
    [a] = published(slack_project, "slack.ping.answered")
    assert a["data"]["rule"] == "reply"                                  # a reply has its own time, read in the same poll


def test_the_within_buckets(slack_project, slack, clock, make_slack):
    slack.user(BOB, "bob")
    waits = {"5m": 200, "15m": 600, "60m": 3000, "over": 5000}
    pings = {}
    for i, (bucket, w) in enumerate(waits.items()):
        pings[bucket] = slack.say(BOB, CHANNEL, T0 + i, mentions=(OWNER,))
        slack.react(CHANNEL, pings[bucket], "white_check_mark", OWNER, T0 + i + w - 60)
    b = make_slack()
    clock.t = T0 + 10
    run_once(b)
    for _ in range(90):
        tick(b, clock)
    assert {k: level(b, "slack_pings_answered_total", within=k) for k in waits} == {"5m": 1, "15m": 1, "60m": 1, "over": 1}
    assert level(b, "slack_pings_unanswered") == 0


def test_a_restart_publishes_nothing_twice_and_loses_no_count(slack_project, slack, clock, make_slack):
    slack.user(BOB, "bob")
    a = slack.say(BOB, CHANNEL, T0 - 100, mentions=(OWNER,))
    slack.say(BOB, CHANNEL, T0 - 50, mentions=(OWNER,))
    slack.react(CHANNEL, a, "white_check_mark", OWNER, T0 + 30)
    b = make_slack()
    run_once(b)
    scrape(b, slack_project, clock.t)
    tick(b, clock)                                         # answered at T0+60, not scraped before the restart
    first = bus(slack_project)
    clock.t += 30
    b2 = make_slack()
    run_once(b2)                                           # resumes from the bus; re-reads the overlap
    scrape(b2, slack_project, clock.t)
    again = bus(slack_project)
    assert [e for e in again if e["source"] != "bridges.slack"] == [e for e in first if e["source"] != "bridges.slack"]
    assert len({e["id"] for e in again}) == len(again)
    assert stored(slack_project, "slack_pings_answered_total", within="5m") == 1      # counted though never scraped before
    assert stored(slack_project, "slack_pings_unanswered") == 1
    assert stored(slack_project, "slack_pings_received_total") == 2


def test_a_restart_continues_counters_the_bus_no_longer_explains(slack_project, slack, clock, make_slack):
    from orchestra.metrics import Point
    slack.user(BOB, "bob")
    slack.say(BOB, CHANNEL, T0 - 100, mentions=(OWNER,))
    b = make_slack()
    run_once(b)
    labels = {"machine": "bridges.slack", "workspace": TEAM, "project": slack_project.key}
    for_project(slack_project).write([Point("slack_pings_received_total", 40.0, clock.t + 1, labels),   # an older bridge's,
                                      Point("slack_pings_answered_total", 9.0, clock.t + 1, {**labels, "within": "15m"})])
    clock.t += 60
    b2 = make_slack()
    run_once(b2)
    assert level(b2, "slack_pings_received_total") == 40 and level(b2, "slack_pings_answered_total", within="15m") == 9
    slack.say(BOB, CHANNEL, clock.t + 5, mentions=(OWNER,))
    tick(b2, clock, slack_project)
    assert level(b2, "slack_pings_received_total") == 41
    for name, extra in COUNTERS.values():
        pts = for_project(slack_project).raw(name, {"machine": "bridges.slack", "workspace": TEAM, **extra})
        assert all(y >= x for (_, x), (_, y) in pairwise(pts)), (name, pts)


def test_a_gap_is_caught_up_and_recorded(slack_project, slack, clock, make_slack):
    slack.user(BOB, "bob")
    b = make_slack()
    run_once(b)
    asleep = slack.say(BOB, CHANNEL, T0 + 1800, mentions=(OWNER,))     # while the laptop slept for two hours
    slack.react(CHANNEL, asleep, "white_check_mark", OWNER, T0 + 2000)
    slack.say(BOB, slack.dm(BOB), T0 + 3600)
    clock.t += 7200
    b.round()
    [gap] = published(slack_project, "bridge.gap")
    assert gap["source"] == "bridges.slack"
    assert gap["data"] == {"workspace": TEAM, "from": "2026-10-26T07:33:20+00:00", "to": "2026-10-26T09:33:20+00:00",
                           "recovered": True}
    got = published(slack_project)
    assert [e["type"] for e in got] == ["bridge.gap", "slack.ping.received", "slack.ping.received", "slack.ping.answered"]
    assert got[1]["time"].startswith("2026-10-26T08:03:20")             # caught up with Slack's own times
    answered = got[3]["data"]
    assert answered["not_before"] == "2026-10-26T08:03:20.000001+00:00"  # the reaction came somewhere in the gap
    assert level(b, "slack_pings_answered_total", within="over") == 1    # stamped when seen: the gap's end


def test_a_gap_longer_than_max_catchup_is_not_recovered(slack_project, slack, clock, make_slack):
    slack.user(BOB, "bob")
    b = make_slack()
    run_once(b)
    slack.say(BOB, CHANNEL, T0 + 3600, mentions=(OWNER,))              # beyond the 6 h it catches up
    late = slack.say(BOB, CHANNEL, T0 + 9 * 3600, mentions=(OWNER,))
    clock.t += 10 * 3600
    b.round()
    assert published(slack_project, "bridge.gap")[0]["data"]["recovered"] is False
    assert [e["data"]["ts"] for e in published(slack_project, "slack.ping.received")] == [late]


def test_a_restart_after_a_gap_records_it_from_the_last_scrape(slack_project, slack, clock, make_slack):
    slack.user(BOB, "bob")
    b = make_slack()
    run_once(b)
    scrape(b, slack_project, clock.t)
    slack.say(BOB, CHANNEL, T0 + 1200, mentions=(OWNER,))
    clock.t += 3600
    run_once(make_slack())
    [gap] = published(slack_project, "bridge.gap")
    assert gap["data"]["from"] == "2026-10-26T07:33:20+00:00" and gap["data"]["recovered"] is True
    assert len(published(slack_project, "slack.ping.received")) == 1


def test_asleep_then_awake_publishes_one_gap_once_it_is_over(slack_project, slack, clock, make_slack, capsys):
    b = make_slack()
    run_once(b)
    slack.asleep = True
    tick(b, clock, seconds=240, times=4)
    assert "poll failed" in capsys.readouterr().err
    assert published(slack_project, "bridge.gap") == []
    slack.asleep = False
    tick(b, clock)
    tick(b, clock)
    [gap] = published(slack_project, "bridge.gap")
    assert (gap["data"]["from"], gap["data"]["to"]) == ("2026-10-26T07:33:20+00:00", "2026-10-26T07:50:20+00:00")


def test_a_rate_limit_is_waited_out_for_its_retry_after(slack_project, slack, clock, make_slack):
    slack.user(BOB, "bob")
    slack.say(BOB, CHANNEL, T0 - 10, mentions=(OWNER,))
    b = make_slack()
    slack.fail_next = [(429, {"Retry-After": "7"}), (429, {"Retry-After": "3"})]
    run_once(b)
    assert b.api.waited == [7.0, 3.0]
    assert len(published(slack_project, "slack.ping.received")) == 1


def test_a_ping_waiting_too_long_expires_and_a_deleted_one_stops_waiting(slack_project, slack, clock, make_slack):
    slack.user(BOB, "bob")
    old = slack.say(BOB, CHANNEL, T0 - 1000, mentions=(OWNER,))
    gone = slack.say(BOB, CHANNEL, T0 - 900, mentions=(OWNER,))
    b = make_slack()
    run_once(b)
    slack.delete(CHANNEL, gone)
    tick(b, clock)
    b.config.expire = 1200.0
    tick(b, clock, times=3)
    got = {e["data"]["ts"]: e for e in published(slack_project, "slack.ping.expired")}
    assert got[gone]["data"]["reason"] == "deleted" and got[gone]["source"] == "bridges.slack"
    assert got[old]["data"]["reason"] == "expired" and got[old]["time"] == "2026-10-26T07:36:40.000001+00:00"
    assert level(b, "slack_pings_expired_total") == 2 and level(b, "slack_pings_unanswered") == 0


def test_many_pings_are_read_across_pages(slack_project, slack, clock, make_slack):
    slack.per_page_max = 2
    slack.user(BOB, "bob")
    for i in range(7):
        slack.say(BOB, CHANNEL, T0 - 100 + i, mentions=(OWNER,))
    run_once(make_slack())
    assert len(published(slack_project, "slack.ping.received")) == 7


def test_metrics_are_served_over_http_and_balance(slack_project, slack, clock, make_slack):
    slack.user(BOB, "bob")
    a = slack.say(BOB, CHANNEL, T0 - 100, mentions=(OWNER,))
    slack.say(BOB, CHANNEL, T0 - 50, mentions=(OWNER,))
    slack.react(CHANNEL, a, "white_check_mark", OWNER, T0 - 10)
    b = make_slack()
    stop = threading.Event()
    rendered = []
    original = b.round

    def round_then_read():
        original()
        rendered.append(urllib.request.urlopen(b.server.url).read().decode())
        with pytest.raises(urllib.error.HTTPError):
            urllib.request.urlopen(b.server.url.replace("/metrics", "/other"))
        stop.set()

    b.round = round_then_read
    b.run(stop)
    text = rendered[0]
    assert 'slack_pings_unanswered{workspace="T0FAKE"} 1' in text
    assert 'slack_pings_received_total{workspace="T0FAKE"} 2' in text
    assert 'slack_pings_answered_total{workspace="T0FAKE",within="5m"} 1' in text
    assert 'slack_pings_expired_total{workspace="T0FAKE"} 0' in text
    with pytest.raises(urllib.error.URLError):
        urllib.request.urlopen(b.server.url, timeout=2)                  # stopped with the bridge


def test_metrics_are_not_served_before_the_bridge_has_read_its_past(slack_project, slack, make_slack):
    b = make_slack()
    assert b.render() is None
    from orchestra_bridges.slack.serve import MetricsServer
    server = MetricsServer(b.render, port=0).start()
    try:
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(server.url)
        assert e.value.code == 503
    finally:
        server.stop()


def test_no_text_and_no_token_anywhere(slack_project, slack, clock, make_slack, capsys, tmp_path, people):
    slack.user(BOB, "bob")
    dm = slack.dm(BOB)
    a = slack.say(BOB, CHANNEL, T0 - 100, mentions=(OWNER,))
    slack.say(BOB, dm, T0 - 90)
    slack.say(OWNER, dm, T0 - 80)
    slack.react(CHANNEL, a, "white_check_mark", OWNER, T0 + 10)
    b = make_slack()
    run_once(b)
    tick(b, clock, slack_project, times=2)
    b.config.answered = ["reply"]
    tick(b, clock, slack_project)
    text = b.render()
    out = capsys.readouterr()
    assert published(slack_project, "slack.ping.answered")
    for where in (json.dumps(bus(slack_project)), text, out.out + out.err):
        assert CANARY not in where and TOKEN not in where
    for f in tmp_path.rglob("*"):
        if f.is_file() and ".git" not in f.parts:
            body = f.read_bytes()
            assert CANARY.encode() not in body and TOKEN.encode() not in body, f
