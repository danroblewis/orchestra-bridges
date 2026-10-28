"""The Jira bridge against the fake Jira (Data Center first, and Cloud): what it publishes, as whom,
once; restarts, gaps, rate limits and failures; never text; the levels it serves."""

import json
import subprocess
import threading
import urllib.request

import pytest
from conftest import T0, Clock
from fake_jira import CANARY, EMAIL, TOKEN, FakeJira
from orchestra.config import load_project
from orchestra.events import Events
from orchestra.metrics import Point, ProjectStore, for_project
from relayed import only_actors

from orchestra_bridges import config as cfg
from orchestra_bridges.jira.api import Jira
from orchestra_bridges.jira.bridge import Bridge
from orchestra_bridges.jira.levels import LAST_POLL

INSTRUMENT = "bridges.jira"
TOML = """[bridges]
people = "people.toml"

[bridges.jira]
api = "{api}"
deployment = "{deployment}"
projects = ["ABC", "OPS"]
every = "60s"
lookback = "1h"
max_catchup = "6h"
overlap = "10m"
port = 0
"""


@pytest.fixture(params=["datacenter"])
def deployment(request):
    return request.param


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def jira(deployment, clock):
    with FakeJira(deployment, clock=clock) as fake:
        yield fake


@pytest.fixture
def project(tmp_path, monkeypatch, jira, deployment):
    monkeypatch.setenv("ORCHESTRATOR_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("ORCHESTRA_EVENTS_URL", raising=False)
    monkeypatch.delenv("ORCHESTRA_METRICS_URL", raising=False)
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    (repo / "orchestra.toml").write_text(TOML.format(api=jira.base, deployment=deployment))
    return load_project(repo)


@pytest.fixture
def sleeps():
    return []


@pytest.fixture
def make(project, jira, clock, sleeps):
    def new():
        conf = cfg.jira(project.config.raw, project.repo_root)
        api = Jira(jira.base, TOKEN, deployment=conf.deployment, email=EMAIL, sleep=sleeps.append)
        return Bridge(conf, api, Events.for_project(project),
                      ProjectStore(for_project(project), project.key), clock=clock)
    return new


def bus(project):
    return Events.for_project(project).after(0)[0]


def jira_events(project):
    return [e for e in bus(project) if e["type"].startswith("jira.")]


def once(b):
    b.run(threading.Event(), once=True)


def gaps(b):
    """The gap counters the bridge serves: (gaps, recovered, seconds)."""
    return tuple(b.gaps.values(b.config.site).values())


def who(jira, login):
    return f"person.jira.{jira.login_of(login).lower()}"


def story(j):
    """Alice files ABC-1 for Bob; Bob starts it; Alice gives it to the owner; Carol comments; Bob
    changes its priority, labels, sprint, links and due date; Alice rewrites its summary (no event);
    the owner finishes it."""
    key = j.create("ABC", "alice", T0 - 3000, assignee="bob", labels=("api",))
    j.transition(key, "bob", T0 - 2900, "3")
    j.assign(key, "alice", T0 - 2800, "owner")
    j.comment(key, "carol", T0 - 2700)
    j.set_priority(key, "bob", T0 - 2600, "2")
    j.label(key, "bob", T0 - 2500, add=("urgent",), remove=("api",))
    j.sprint(key, "bob", T0 - 2400, 12)
    j.link(key, "bob", T0 - 2300, "OPS-7")
    j.due(key, "bob", T0 - 2200, "2026-11-02")
    j.edit_text(key, "alice", T0 - 2100)
    j.transition(key, "owner", T0 - 2000, "10002")
    return key


@pytest.mark.parametrize("deployment", ["datacenter", "cloud"], indirect=True)
def test_each_event_is_published_once_as_who_acted_on_the_issue(project, jira, make, clock):
    key = story(jira)
    b = make()
    once(b)
    got = [(e["type"], e["source"], e["subject"]) for e in jira_events(project)]
    a, bob, c, me = (who(jira, x) for x in ("alice", "bob", "carol", "owner"))
    assert got == [("jira.issue.created", a, key), ("jira.issue.assigned", a, key),
                   ("jira.issue.transitioned", bob, key), ("jira.issue.assigned", a, key),
                   ("jira.comment.added", c, key)] + [("jira.issue.updated", bob, key)] * 5 + [
                   ("jira.issue.transitioned", me, key)]
    clock.t += 60
    b.round()
    assert len(jira_events(project)) == len(got)                        # a second poll publishes nothing again
    evs = jira_events(project)
    assert evs[1]["data"]["assignee"] == jira.login_of("bob") and evs[1]["data"]["previous"] is None
    assert evs[3]["data"]["assignee"] == jira.login_of("owner") and evs[3]["data"]["previous"] == jira.login_of("bob")
    assert evs[3]["data"]["assignee_machine"] == me
    t = evs[2]["data"]
    assert (t["from"], t["to"], t["from_category"], t["to_category"]) == ("Open", "In Progress", "new", "indeterminate")
    assert evs[-1]["data"]["to_category"] == "done"
    assert evs[4]["data"]["comments"] == 1 and evs[4]["data"]["author"] == jira.login_of("carol")
    changes = [e["data"]["changes"][0] for e in evs if e["type"] == "jira.issue.updated"]
    assert changes == [{"field": "priority", "from": "3", "to": "2"},
                       {"field": "labels", "added": ["urgent"], "removed": ["api"]},
                       {"field": "sprint", "from": [], "to": [12]},
                       {"field": "link", "from": None, "to": "OPS-7"},
                       {"field": "duedate", "from": None, "to": "2026-11-02"}]
    d = evs[0]["data"]
    assert (d["project"], d["key"], d["issuetype"], d["status"], d["category"]) == ("ABC", key, "Task", "Open", "new")
    assert (d["sprints"], d["links"], d["labels"], d["priority"]) == ([12], ["OPS-7"], ["urgent"], "High")
    assert evs[0]["time"] == "2026-10-26T06:43:20+00:00"
    assert all(e["id"].startswith(f"jira:{b.config.site}:issue:{jira.issues[key]['id']}:") for e in evs)
    machines = only_actors(bus(project))
    assert {m["id"] for m in machines} == {a, bob, c, me}


def test_a_data_center_user_key_in_a_change_is_published_as_the_username(project, jira, make):
    key = jira.create("ABC", "bob", T0 - 300)
    jira.assign(key, "bob", T0 - 200, "alice")                      # the changelog names alice by JIRAUSER…
    assert jira.account("alice").startswith("JIRAUSER")
    jira.comment(key, "alice", T0 - 100)                              # so the issue shows her key and name
    once(make())
    [assigned] = [e for e in jira_events(project) if e["type"] == "jira.issue.assigned"]
    assert assigned["data"]["assignee"] == "alice" and assigned["data"]["assignee_machine"] == "person.jira.alice"


def test_never_text_in_events_logs_metrics_or_files(project, jira, make, capsys, tmp_path):
    story(jira)
    k = jira.create("OPS", "carol", T0 - 500, assignee="owner")
    jira.comment(k, "dave", T0 - 400, restricted=True)
    b = make()
    once(b)
    raw = json.dumps(bus(project))
    assert jira_events(project) and CANARY not in raw and TOKEN not in raw
    assert CANARY not in b.render() and TOKEN not in b.render()
    err = capsys.readouterr()
    assert CANARY not in err.out + err.err and TOKEN not in err.out + err.err
    for f in tmp_path.rglob("*"):
        if f.is_file():
            body = f.read_bytes()
            assert CANARY.encode() not in body and TOKEN.encode() not in body, f
    restricted = [e for e in jira_events(project) if e["type"] == "jira.comment.added" and e["subject"] == k]
    assert restricted[0]["data"]["restricted"] is True


def test_a_restart_neither_duplicates_nor_loses(project, jira, make, clock):
    key = story(jira)
    once(make())
    first = jira_events(project)
    jira.comment(key, "dave", T0 + 30)                                # while the bridge is stopped
    jira.transition(key, "dave", T0 + 40, "1")
    clock.t += 120
    once(make())
    again = jira_events(project)
    assert again[:len(first)] == first
    assert [(e["type"], e["source"]) for e in again[len(first):]] == [
        ("jira.comment.added", who(jira, "dave")), ("jira.issue.transitioned", who(jira, "dave"))]
    assert len({e["id"] for e in again}) == len(again)
    clock.t += 60
    once(make())
    assert jira_events(project) == again
    only_actors(bus(project))


def test_after_the_laptop_sleeps_it_catches_up_once_and_records_the_gap(project, jira, make, clock):
    key = jira.create("ABC", "alice", T0 - 300)
    b = make()
    once(b)
    jira.asleep = True
    jira.comment(key, "bob", T0 + 1800)
    jira.transition(key, "bob", T0 + 3600, "3")
    for _ in range(3):                                                # polls fail and publish nothing
        clock.t += 1200
        b.round()
    assert gaps(b) == (0, 0, 0)
    jira.asleep = False
    clock.t += 1200
    b.round()
    assert gaps(b) == (1, 1, 4800.0)                                 # from T0 to now, counted once
    assert not [e for e in bus(project) if e["type"] == "bridge.gap"]
    text = b.render()
    assert "jira_bridge_gaps_total 1\n" in text and "jira_bridge_gap_seconds_total 4800\n" in text
    late = jira_events(project)[1:]
    assert [(e["type"], e["time"]) for e in late] == [("jira.comment.added", "2026-10-26T08:03:20+00:00"),
                                                       ("jira.issue.transitioned", "2026-10-26T08:33:20+00:00")]


def test_a_gap_longer_than_max_catchup_is_not_recovered(project, jira, make, clock):
    key = jira.create("ABC", "alice", T0 - 300)
    b = make()
    once(b)
    jira.comment(key, "bob", T0 + 3600)                               # beyond the 6 h it will catch up
    jira.comment(key, "carol", T0 + 9 * 3600)
    clock.t += 10 * 3600
    b.round()
    assert gaps(b) == (1, 0, 36000.0)
    assert [e["source"] for e in jira_events(project) if e["type"] == "jira.comment.added"] == [who(jira, "carol")]


def test_a_restart_finds_its_last_good_poll_in_the_store_and_records_the_gap(project, jira, make, clock):
    jira.create("ABC", "alice", T0 - 300)
    once(make())
    labels = {"instrument": INSTRUMENT}
    ProjectStore(for_project(project), project.key).write([Point(LAST_POLL, T0, T0, labels),
                                                          Point("jira_bridge_gaps_total", 2, T0, labels),
                                                          Point("jira_bridge_gaps_recovered_total", 2, T0, labels)])
    clock.t += 3 * 3600
    b = make()
    once(b)
    assert gaps(b) == (3, 3, 3 * 3600.0)                             # counted on from what was stored


def test_a_429_is_honoured_and_the_poll_goes_on(project, jira, make, sleeps):
    jira.create("ABC", "alice", T0 - 300)
    jira.fail_next = [(429, {"Retry-After": "7"}), (503, {"Retry-After": "2"})]
    once(make())
    assert sleeps == [7.0, 2.0]
    assert [e["type"] for e in jira_events(project)] == ["jira.issue.created"]


def test_a_poll_that_fails_part_way_publishes_nothing(project, jira, make, clock):
    key = jira.create("ABC", "alice", T0 - 300)
    b = make()
    once(b)
    jira.comment(key, "bob", T0 + 10)
    jira.fail_next = [(500, {})]
    clock.t += 60
    b.round()
    assert len(jira_events(project)) == 1
    clock.t += 60
    b.round()
    assert len(jira_events(project)) == 2


@pytest.mark.parametrize("deployment", ["datacenter", "cloud"], indirect=True)
def test_long_searches_changelogs_and_comment_lists_are_read_whole(project, jira, make):
    jira.page_max, jira.changelog_max, jira.comment_max = 2, 1, 1
    keys = [jira.create("ABC", "alice", T0 - 600 + n) for n in range(5)]
    for n in range(3):
        jira.transition(keys[0], "bob", T0 - 500 + n, "3" if n % 2 == 0 else "1")
        jira.comment(keys[0], "carol", T0 - 400 + n)
    once(make())
    types = [e["type"] for e in jira_events(project)]
    assert types.count("jira.issue.created") == 5 and types.count("jira.issue.transitioned") == 3
    assert [e["data"]["comments"] for e in jira_events(project) if e["type"] == "jira.comment.added"] == [1, 2, 3]
    paths = [p.removeprefix(jira.prefix) for p, *_ in jira.requests]
    whole = "/issue/ABC-1/changelog" if jira.cloud else "/issue/ABC-1"
    assert whole in paths and "/issue/ABC-1/comment" in paths                 # the rest was read from the issue
    assert sum(p.startswith("/search") for p in paths) > 4                    # pages of two


def test_people_map_through_the_identity_map_with_their_teams(project, jira, make):
    (project.repo_root / "people.toml").write_text(
        f'[people.al]\nname = "Alice L"\nteam = "web"\njira = "{jira.account("alice")}"\n'      # her Data Center user key
        '[people.dan]\nname = "Daniel"\nteam = "sre"\njira = "owner"\ngithub = "danroblewis"\n'
        '[people.robot]\nname = "Automation"\nbot = true\njira = "automation"\n')
    key = jira.create("ABC", "alice", T0 - 300, assignee="owner")
    jira.comment(key, "carol", T0 - 250)
    jira.transition(key, "automation", T0 - 200, "3")
    jira.transition(key, None, T0 - 100, "10001")                     # Jira's own change: no author
    once(make())
    got = [(e["type"], e["source"]) for e in jira_events(project)]
    assert got == [("jira.issue.created", "person.al"), ("jira.issue.assigned", "person.al"),
                   ("jira.comment.added", "person.jira.carol"), ("jira.issue.transitioned", "bot.robot"),
                   ("jira.issue.transitioned", "bot.jira")]
    assert jira_events(project)[1]["data"]["assignee_machine"] == "person.dan"
    started = {e["source"]: e["data"] for e in bus(project) if e["type"] == "orchestra.machine.started"}
    assert (started["person.al"]["name"], started["person.al"]["team"]) == ("Alice L", "web")
    assert started["person.jira.carol"]["name"] == "Carol Person" and "team" not in started["person.jira.carol"]
    assert started["bot.robot"]["kind"] == "program"
    assert {k: started["bot.jira"][k] for k in ("kind", "name", "bot")} == {"kind": "program", "name": "Jira", "bot": True}
    assert "jira" not in started["bot.jira"]                         # no account behind it
    only_actors(bus(project))


@pytest.mark.parametrize("deployment", ["datacenter", "cloud"], indirect=True)
def test_a_change_with_no_author_is_credited_to_jira_itself(project, jira, make, clock):
    key = jira.create("ABC", "alice", T0 - 300)
    jira.transition(key, None, T0 - 200, "3")                         # automation, a post-function: no author
    jira.transition(key, None, T0 - 100, "10002")
    b = make()
    once(b)
    moved = [e for e in jira_events(project) if e["type"] == "jira.issue.transitioned"]
    assert [e["source"] for e in moved] == ["bot.jira", "bot.jira"] and moved[0]["data"]["login"] is None
    started = [e for e in bus(project) if e["type"] == "orchestra.machine.started" and e["source"] == "bot.jira"]
    assert len(started) == 1 and started[0]["id"].startswith("jira:announce:bot.jira:")
    clock.t += 60
    once(make())                                                       # a restart announces it no more
    assert len([e for e in bus(project) if e["source"] == "bot.jira" and e["type"] == "orchestra.machine.started"]) == 1
    machines = {m["id"]: m for m in only_actors(bus(project))}
    assert machines["bot.jira"]["kind"] == "program" and machines["bot.jira"]["name"] == "Jira"


@pytest.mark.parametrize("deployment", ["datacenter", "cloud"], indirect=True)
def test_a_status_restated_at_creation_is_no_transition(project, jira, make):
    key = jira.create("ABC", "alice", T0 - 300)
    jira.restate(key, "alice", T0 - 299.4)                               # Cloud: To Do to To Do, by id
    jira.transition(key, "bob", T0 - 200, "3")
    once(make())
    got = [(e["type"], e["data"].get("from_id"), e["data"].get("to_id")) for e in jira_events(project)]
    assert got == [("jira.issue.created", None, None), ("jira.issue.transitioned", "1", "3")]
    assert jira_events(project)[0]["data"]["status_id"] == "1"


def test_the_owner_not_in_the_identity_map_is_told_how_to_list_themselves(project, jira, make, capsys):
    once(make())
    assert 'list them as jira = "owner" (their username)' in capsys.readouterr().err
    (project.repo_root / "people.toml").write_text('[people.me]\njira = "owner"\n')
    once(make())
    assert "identity map" not in capsys.readouterr().err


@pytest.mark.parametrize("deployment", ["cloud"], indirect=True)
def test_a_cloud_app_account_is_a_bot(project, jira, make):
    jira.apps.add("automation")
    key = jira.create("ABC", "alice", T0 - 300)
    jira.transition(key, "automation", T0 - 200, "3")
    once(make())
    assert jira_events(project)[-1]["source"] == "bot.jira.acc-automation"


def test_the_levels_say_what_waits(project, jira, make, clock):
    a = jira.create("ABC", "alice", T0 - 9000, assignee="owner")       # mine, not started
    b_ = jira.create("ABC", "alice", T0 - 9000, assignee="owner")
    jira.transition(b_, "owner", T0 - 8000, "3")                        # mine, started
    jira.comment(b_, "bob", T0 - 1800)                                   # waits for the owner half an hour
    jira.comment(b_, "carol", T0 - 600)
    c = jira.create("OPS", "alice", T0 - 9000, assignee="owner")
    jira.comment(c, "bob", T0 - 900)
    jira.comment(c, "owner", T0 - 300)                                   # answered
    jira.create("OPS", "alice", T0 - 9000, assignee="bob")
    d = jira.create("OPS", "alice", T0 - 9000)
    jira.transition(d, "bob", T0 - 8000, "10002")                        # done
    jira.comment(a, "automation", T0 - 100)
    (project.repo_root / "people.toml").write_text('[people.robot]\nbot = true\njira = "automation"\n')
    b = make()
    assert b.render() is None
    once(b)
    text = b.render()
    for line in ['jira_issues{jira_project="ABC",category="new"} 1', 'jira_issues{jira_project="ABC",category="indeterminate"} 1',
                 'jira_issues{jira_project="OPS",category="new"} 2', 'jira_issues{jira_project="OPS",category="done"} 1',
                 'jira_issues_mine_not_started{jira_project="ABC"} 1', 'jira_issues_mine_not_started{jira_project="OPS"} 1',
                 'jira_comments_unanswered{jira_project="ABC"} 1', 'jira_comments_unanswered{jira_project="OPS"} 0',
                 'jira_comment_unanswered_oldest_seconds{jira_project="ABC"} 1800',
                 f"jira_bridge_last_poll_timestamp_seconds {int(T0)}"]:
        assert line + "\n" in text, line
    jira.comment(b_, "owner", T0 + 30)
    clock.t += 60
    b.round()
    assert 'jira_comments_unanswered{jira_project="ABC"} 0\n' in b.render()


def test_it_serves_metrics_while_it_runs(project, jira, make):
    jira.create("ABC", "alice", T0 - 300)
    b = make()
    stop = threading.Event()
    t = threading.Thread(target=b.run, args=(stop,), daemon=True)
    t.start()
    try:
        for _ in range(100):
            if b.server and b.levels is not None:
                break
            stop.wait(0.05)
        with urllib.request.urlopen(b.server.url, timeout=5) as r:
            assert 'jira_issues{jira_project="ABC",category="new"} 1' in r.read().decode()
    finally:
        stop.set()
        t.join(timeout=10)
