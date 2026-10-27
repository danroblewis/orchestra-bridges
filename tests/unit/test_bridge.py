import json
from itertools import pairwise

from conftest import T0, bus, published, run_once
from fake_github import CANARY, TOKEN
from orchestra.metrics import for_project

R = "microsoft/vscode"


def busy(g):
    """Alice opens #1, a bot labels it, Bob answers 5 minutes in, Alice closes it; Carol opens #2 and
    nobody answers; a pull request and its comment are left out."""
    g.open(1, "alice", T0 - 1800)
    g.event(1, "labeled", "vscode-triage-bot", T0 - 1790, label="bug", bot=True)
    g.comment(1, "bob", T0 - 1500)
    g.event(1, "closed", "alice", T0 - 600)
    g.open(2, "carol", T0 - 300)
    g.open(3, "dave", T0 - 200, pull=True)
    g.comment(3, "bob", T0 - 100)


def level(project, series, **labels):
    last = for_project(project).latest(series, {"machine": "bridges.github", "repo": R, **labels})
    return last[1] if last else None


def test_a_poll_publishes_what_people_did_as_them(project, github, make_bridge):
    busy(github)
    run_once(make_bridge())
    evs = published(project)
    assert [(e["type"], e["source"], e["subject"]) for e in evs] == [
        ("github.issue.opened", "person.alice", f"{R}#1"), ("github.issue.labelled", "bot.vscode-triage-bot", f"{R}#1"),
        ("github.issue.commented", "person.bob", f"{R}#1"), ("github.issue.closed", "person.alice", f"{R}#1"),
        ("github.issue.opened", "person.carol", f"{R}#2")]
    assert evs[0]["id"] == f"github:{R}:opened:1"
    assert evs[0]["time"] == "2026-10-26T07:03:20+00:00"           # GitHub's time, not the poll's
    assert evs[0]["data"] == {"repository": R, "issue": 1, "login": "alice", "association": "NONE"}


def test_each_actor_is_announced_once_before_their_first_event(project, github, make_bridge):
    busy(github)
    run_once(make_bridge())
    announced = [e for e in bus(project) if e["type"] == "orchestra.machine.started"]
    assert [e["source"] for e in announced] == ["bridges.github", "person.alice", "bot.vscode-triage-bot",
                                                "person.bob", "person.carol"]
    assert announced[0]["data"]["package"] == "bridges" and announced[0]["data"]["mode"] == "continuous"
    assert announced[2]["data"]["kind"] == "program" and announced[1]["data"]["kind"] == "person"
    order = [e["source"] for e in bus(project)]
    assert order.index("person.bob") < next(i for i, e in enumerate(bus(project)) if e["type"] == "github.issue.commented")


def test_the_identity_map_names_people_and_their_teams(project, github, make_bridge):
    (project.repo_root / "people.toml").write_text('[people.al]\nname = "Alice Liddell"\nteam = "editor"\ngithub = "alice"\n')
    busy(github)
    run_once(make_bridge())
    assert published(project, "github.issue.opened")[0]["source"] == "person.al"
    [a] = [e for e in bus(project) if e["source"] == "person.al" and e["type"] == "orchestra.machine.started"]
    assert (a["data"]["name"], a["data"]["team"], a["data"]["github"]) == ("Alice Liddell", "editor", "alice")


def test_overlapping_polls_and_restarts_publish_nothing_twice(project, github, clock, make_bridge):
    busy(github)
    b = make_bridge()
    run_once(b)
    first = bus(project)
    clock.t += 60
    b.round()
    b2 = make_bridge()                                 # a restart: it resumes from the bus
    clock.t += 600                                     # past overlap: everything read is counted
    run_once(b2)
    again = bus(project)
    assert [e for e in again if e["source"] != "bridges.github"] == [e for e in first if e["source"] != "bridges.github"]
    assert len({e["id"] for e in again}) == len(again)
    assert b2.responses[R].waiting.keys() == {f"{R}#2"}   # rebuilt from the bus


def test_first_response_counts(project, github, clock, make_bridge):
    busy(github)
    b = make_bridge()
    run_once(b)
    assert level(project, "github_issues_unanswered") == 0     # carol's issue, opened 5 minutes ago, is not counted yet
    for _ in range(5):                                         # five minutes on, it is older than overlap (10 minutes)
        clock.t += 60
        b.round()
    assert level(project, "github_issues_unanswered") == 1
    assert level(project, "github_issues_opened_total") == 2
    assert level(project, "github_issues_answered_total", within="15m") == 1
    assert level(project, "github_issue_first_response_seconds_total") == 300.0
    assert level(project, "github_issues_unanswered_oldest_seconds") == 600.0
    github.comment(2, "erin", T0 + 330)
    clock.t += 60
    b.round()
    assert level(project, "github_issues_unanswered") == 1     # read and published, counted only past overlap
    for _ in range(10):
        clock.t += 60
        b.round()
    assert level(project, "github_issues_unanswered") == 0
    assert level(project, "github_issues_answered_total", within="15m") == 2


def test_points_are_never_written_behind_ones_already_written(project, github, clock, make_bridge):
    busy(github)
    b = make_bridge()
    run_once(b)
    github.comment(2, "erin", T0 - 20)                 # happened before the last poll, seen only now
    clock.t += 60
    b.round()
    pts = for_project(project).raw("github_issues_unanswered", {"machine": "bridges.github", "repo": R})
    assert [t for t, _ in pts] == sorted(t for t, _ in pts)
    assert pts[-2] == (T0, 0.0) and pts[-1] == (T0 + 60, 0.0)


def test_after_a_gap_it_catches_up_with_the_events_own_times(project, github, clock, make_bridge):
    b = make_bridge()
    run_once(b)
    github.open(5, "frank", T0 + 1800)                 # while the laptop slept for two hours
    github.comment(5, "gina", T0 + 3600)
    clock.t += 7200
    b.round()
    [gap] = published(project, "bridge.gap")
    assert gap["data"]["recovered"] is True and gap["data"]["from"] == "2026-10-26T07:33:20Z"
    assert [e["time"] for e in published(project) if e["type"].startswith("github.issue.")] == [
        "2026-10-26T08:03:20+00:00", "2026-10-26T08:33:20+00:00"]
    pts = for_project(project).raw("github_issues_unanswered", {"machine": "bridges.github", "repo": R})
    assert (T0 + 1800, 1.0) in pts and (T0 + 3600, 0.0) in pts


def test_a_gap_longer_than_max_catchup_is_recorded_as_not_recovered(project, github, clock, make_bridge):
    b = make_bridge()
    run_once(b)
    github.open(6, "old", T0 + 3600)                   # beyond the 6 h it will catch up
    github.open(7, "new", T0 + 9 * 3600)
    clock.t += 10 * 3600
    b.round()
    assert published(project, "bridge.gap")[0]["data"]["recovered"] is False
    assert [e["subject"] for e in published(project, "github.issue.opened")] == [f"{R}#7"]


def test_a_restart_after_a_gap_records_it_and_catches_up(project, github, clock, make_bridge):
    run_once(make_bridge())
    github.open(8, "hal", T0 + 1200)
    clock.t += 3600
    run_once(make_bridge())
    assert published(project, "bridge.gap")[0]["data"]["recovered"] is True
    assert [e["subject"] for e in published(project, "github.issue.opened")] == [f"{R}#8"]


def test_no_text_and_no_token_reach_the_bus_the_store_or_the_log(project, github, clock, make_bridge, capsys):
    busy(github)
    b = make_bridge()
    run_once(b)
    clock.t += 60
    b.round()
    metrics = (project.state_dir / "metrics.jsonl").read_text()
    out = capsys.readouterr()
    for where in (json.dumps(bus(project)), metrics, out.out + out.err):
        assert CANARY not in where and TOKEN not in where


def test_a_failed_poll_is_logged_and_the_next_round_tries_again(project, github, clock, make_bridge, capsys):
    b = make_bridge()
    b.resume()
    github.fail_next = [(502, {})]
    b.round()
    assert "poll failed" in capsys.readouterr().err
    busy(github)
    clock.t += 60
    b.round()
    assert len(published(project, "github.issue.opened")) == 2


def test_an_outage_publishes_one_gap_once_it_is_over(project, github, clock, make_bridge):
    b = make_bridge()
    run_once(b)                                        # the last good poll, at T0
    for _ in range(5):                                 # GitHub is down for five rounds, several minutes apart
        clock.t += 240
        github.fail_next = [(502, {})]
        b.round()
    assert published(project, "bridge.gap") == []
    clock.t += 240
    b.round()                                          # it answers again
    clock.t += 60
    b.round()
    [gap] = published(project, "bridge.gap")
    assert (gap["data"]["from"], gap["data"]["to"]) == ("2026-10-26T07:33:20Z", "2026-10-26T07:57:20Z")
    assert gap["data"]["recovered"] is True


def counters_never_fall(project):
    from orchestra_bridges.github.responses import COUNTERS
    store = for_project(project)
    for name, extra in COUNTERS.values():
        pts = store.raw(name, {"machine": "bridges.github", "repo": R, **extra})
        assert all(b >= a for (_, a), (_, b) in pairwise(pts)), (name, extra, pts)


def test_a_comment_github_shows_late_is_counted_in_time_order_and_a_restart_agrees(project, github, clock, make_bridge):
    github.open(1, "alice", T0 - 2000)
    github.event(1, "closed", "alice", T0 - 300)       # alice closes it; read at T0
    b = make_bridge()
    run_once(b)
    github.comment(1, "bob", T0 - 400)                 # bob's comment, made before the close, shows up a minute later
    for _ in range(12):
        clock.t += 60
        b.round()
    assert level(project, "github_issues_answered_total", within="1h") == 1
    assert level(project, "github_issues_closed_unanswered_total") == 0
    clock.t += 60
    run_once(make_bridge())                            # a restart rebuilds the same counts
    clock.t += 60
    run_once(make_bridge())
    assert level(project, "github_issues_answered_total", within="1h") == 1
    assert level(project, "github_issues_closed_unanswered_total") == 0
    counters_never_fall(project)


def test_a_restart_continues_every_counter_from_what_was_written(project, github, clock, make_bridge):
    busy(github)
    b = make_bridge()
    run_once(b)
    clock.t += 600
    b.round()
    from orchestra.metrics import Point
    labels = {"machine": "bridges.github", "repo": R, "project": project.key}
    store = for_project(project)                       # what an earlier bridge wrote, from events the bus no longer holds
    store.write([Point("github_issues_opened_total", 40.0, clock.t + 1, labels),
                 Point("github_issues_closed_unanswered_total", 7.0, clock.t + 1, labels)])
    clock.t += 60
    b2 = make_bridge()
    run_once(b2)
    assert level(project, "github_issues_opened_total") == 40.0
    github.open(9, "zoe", clock.t + 10)
    clock.t += 700
    b2.round()
    assert level(project, "github_issues_opened_total") == 41.0
    assert level(project, "github_issues_closed_unanswered_total") == 7.0
    counters_never_fall(project)


def test_a_changed_team_is_announced_again_on_start_without_a_new_event(project, github, clock, make_bridge):
    people = project.repo_root / "people.toml"
    people.write_text('[people.al]\nname = "Alice Liddell"\nteam = "editor"\ngithub = "alice"\n'
                      '[people.ed]\nname = "Ed"\nteam = "nobody-saw-him"\ngithub = "ed"\n')
    busy(github)
    clock.t += 600
    run_once(make_bridge())
    people.write_text('[people.al]\nname = "Alice Liddell"\nteam = "terminal"\ngithub = "alice"\n'
                      '[people.ed]\nname = "Ed"\nteam = "other"\ngithub = "ed"\n')
    clock.t += 60
    run_once(make_bridge())                              # nothing new happened on GitHub
    clock.t += 60
    run_once(make_bridge())                              # and announced only once
    al = [e["data"]["team"] for e in bus(project) if e["source"] == "person.al" and e["type"] == "orchestra.machine.started"]
    assert al == ["editor", "terminal"]
    assert not [e for e in bus(project) if e["source"] == "person.ed"]      # never seen: not invented as a machine


def test_a_restart_reads_events_published_under_the_old_types(project, github, clock, make_bridge):
    from orchestra.events import Events

    from orchestra_bridges.github.poll import iso
    github.open(1, "alice", T0 - 3000)
    c = github.comment(1, "bob", T0 - 2900)
    events = Events.for_project(project)              # what an older bridge published, before the types had a prefix
    for type_, src, t, n in (("issue.opened", "person.alice", T0 - 3000, "opened:1"),
                             ("issue.commented", "person.bob", T0 - 2900, f"comment:{c['id']}")):
        events.publish(type_, {}, source=src, subject=f"{R}#1", time=iso(t), id=f"github:{R}:{n}")
    run_once(make_bridge())
    assert [e["type"] for e in published(project)] == ["issue.opened", "issue.commented"]     # nothing published twice
    assert level(project, "github_issues_answered_total", within="15m") == 1
    assert level(project, "github_issues_opened_total") == 1
