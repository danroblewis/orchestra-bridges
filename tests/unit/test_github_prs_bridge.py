"""The pull-request bridge against the fake GitHub: what it publishes, as whom, once; restarts, gaps
and failures; the levels it serves."""

import json
import subprocess
import threading
import urllib.request

import pytest
from conftest import T0, Clock
from fake_github import CANARY, TOKEN
from fake_github_prs import FakePRs
from orchestra.config import load_project
from orchestra.events import Events
from orchestra.metrics import Point, ProjectStore, for_project

from orchestra_bridges import config as cfg
from orchestra_bridges.github.api import GitHub
from orchestra_bridges.github_prs.bridge import INSTRUMENT, MACHINE, Bridge
from orchestra_bridges.github_prs.levels import LAST_POLL

R = "acme/service"
TOML = """[bridges]
people = "people.toml"

[bridges.github_prs]
repos = ["acme/service"]
branches = ["agent/*"]
labels = ["follow-me"]
every = "60s"
lookback = "1h"
max_catchup = "6h"
overlap = "10m"
port = 0
api = "{api}"

[bridges.github_prs.label_results]
SAIL = ["SAIL-passed", "SAIL-failed"]
"""


@pytest.fixture
def gh():
    with FakePRs(R) as fake:
        yield fake


@pytest.fixture
def project(tmp_path, monkeypatch, gh):
    monkeypatch.setenv("ORCHESTRATOR_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("ORCHESTRA_EVENTS_URL", raising=False)
    monkeypatch.delenv("ORCHESTRA_METRICS_URL", raising=False)
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    (repo / "orchestra.toml").write_text(TOML.format(api=gh.base))
    return load_project(repo)


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def make(project, gh, clock):
    def new():
        conf = cfg.github_prs(project.config.raw, project.repo_root)
        return Bridge(conf, GitHub(TOKEN, gh.base, sleep=lambda s: None, clock=clock),
                      Events.for_project(project, source=MACHINE), ProjectStore(for_project(project), project.key),
                      clock=clock)
    return new


def bus(project):
    return Events.for_project(project).after(0)[0]


def pr_events(project):
    return [e for e in bus(project) if e["type"].startswith("github.pr.")]


def once(b):
    b.run(threading.Event(), once=True)


def lifecycle(g):
    """Alice opens #7 from agent/fix-login and asks Bob to review; Bob requests changes with two line
    comments; Carol comments; Alice pushes; the pipeline's bot reports SAIL failed; a check fails and
    a status passes; Bob approves; Alice merges."""
    g.open(7, "alice", T0 - 3000, branch="agent/fix-login", committer="alice")
    g.label(7, "SAIL", "alice", T0 - 2950)
    g.request_review(7, "alice", "bob", T0 - 2900)
    g.review(7, "bob", T0 - 2800, "CHANGES_REQUESTED", comments=(("src/login.py", 12), ("src/auth.py", 40)))
    g.comment(7, "carol", T0 - 2700)
    g.push(7, "alice", T0 - 2600)
    g.label(7, "SAIL-failed", "sail-bot", T0 - 2500)
    g.check(7, "build", "failure", T0 - 2400)
    g.status(7, "ci/sail", "success", "sail-bot", T0 - 2300)
    g.unlabel(7, "SAIL-failed", "alice", T0 - 2200)
    g.review(7, "bob", T0 - 2100, "APPROVED")
    g.merge(7, "alice", T0 - 2000)


def test_a_pull_request_to_merge_is_published_as_the_people_who_acted(project, gh, make):
    lifecycle(gh)
    once(make())
    got = [(e["type"], e["source"], e["subject"]) for e in pr_events(project)]
    s = f"{R}#7"
    assert got == [
        ("github.pr.opened", "person.alice", s), ("github.pr.labelled", "person.alice", s),
        ("github.pr.review_requested", "person.alice", s), ("github.pr.review_submitted", "person.bob", s),
        ("github.pr.review_commented", "person.bob", s), ("github.pr.review_commented", "person.bob", s),
        ("github.pr.commented", "person.carol", s), ("github.pr.pushed", "person.alice", s),
        ("github.pr.labelled", "person.sail-bot", s), ("github.pr.check_completed", "bot.github-actions", s),
        ("github.pr.check_completed", "person.sail-bot", s), ("github.pr.unlabelled", "person.alice", s),
        ("github.pr.review_submitted", "person.bob", s), ("github.pr.merged", "person.alice", s)]
    by = {e["type"]: e for e in pr_events(project)}
    review = next(e for e in pr_events(project) if e["type"] == "github.pr.review_submitted")
    assert review["data"]["state"] == "changes_requested" and review["data"]["comments"] == 2
    assert review["data"]["head"] != gh.head(7)                    # the commit reviewed, before the push
    line = by["github.pr.review_commented"]
    assert {k: line["data"][k] for k in ("path", "line", "side", "review")} == {
        "path": "src/auth.py", "line": 40, "side": "RIGHT", "review": review["data"]["review"]}
    assert by["github.pr.pushed"]["data"]["head"] == gh.head(7) and by["github.pr.pushed"]["data"]["pusher_from"] == "committer"
    assert by["github.pr.merged"]["id"] == f"github:{R}:pr:7:event:{gh.timelines[7][-2]['id']}"
    assert "github.pr.closed" not in by                             # the merge's own close is not a close


def test_every_event_carries_what_reengaging_a_worker_needs(project, gh, make, clock):
    gh.open(3, "alice", T0 - 300, branch="agent/x", committer="alice")
    gh.review(3, "bob", T0 - 200, "CHANGES_REQUESTED", comments=(("a.py", 1),))
    head = gh.head(3)
    once(make())
    for e in pr_events(project):
        d = e["data"]
        assert (d["repository"], d["pull"], d["branch"], d["base"], d["head"]) == (R, 3, "agent/x", "main", head)
        assert d["login"] and e["source"] and e["subject"] == f"{R}#3"
        assert e["id"].startswith(f"github:{R}:pr:3:")


def test_checks_and_statuses_on_an_open_head(project, gh, make):
    gh.open(4, "alice", T0 - 300, branch="agent/x")
    gh.check(4, "build", "failure", T0 - 200)
    gh.check(4, "lint", None, T0 - 100)                     # still running: nothing yet
    gh.status(4, "ci/sail", "pending", "sail-bot", T0 - 90)
    gh.status(4, "ci/sail", "failure", "sail-bot", T0 - 60)
    once(make())
    checks = [e for e in pr_events(project) if e["type"] == "github.pr.check_completed"]
    assert [(e["source"], e["data"]["check"], e["data"]["conclusion"], e["data"]["via"]) for e in checks] == [
        ("bot.github-actions", "build", "failure", "check_run"), ("person.sail-bot", "ci/sail", "failure", "status")]


def test_never_text_and_never_the_token(project, gh, make):
    lifecycle(gh)
    once(make())
    raw = json.dumps(bus(project))
    assert CANARY not in raw and TOKEN not in raw and pr_events(project)


def test_only_followed_pull_requests_and_a_label_starts_following(project, gh, make, clock):
    gh.open(1, "alice", T0 - 300, branch="feature/other")           # neither the branch nor the label
    gh.open(2, "bob", T0 - 300, branch="agent/two")                   # the branch matches
    b = make()
    once(b)
    assert {e["subject"] for e in pr_events(project)} == {f"{R}#2"}
    gh.label(1, "follow-me", "alice", T0 + 30)
    clock.t += 60
    b.round()
    assert [(e["type"], e["subject"]) for e in pr_events(project)][-2:] == [
        ("github.pr.opened", f"{R}#1"), ("github.pr.labelled", f"{R}#1")]
    gh.unlabel(1, "follow-me", "alice", T0 + 90)                    # followed until resolved, whatever its labels
    clock.t += 60
    b.round()
    assert pr_events(project)[-1]["type"] == "github.pr.unlabelled"


def test_an_old_closed_pull_request_is_not_history_to_publish(project, gh, make):
    gh.open(1, "alice", T0 - 86400, branch="agent/old")
    gh.merge(1, "alice", T0 - 600)                                  # merged in the window, opened long before
    gh.open(2, "bob", T0 - 1200, branch="agent/new")
    gh.merge(2, "bob", T0 - 300)
    once(make())
    assert [(e["type"], e["subject"]) for e in pr_events(project)] == [
        ("github.pr.opened", f"{R}#2"), ("github.pr.merged", f"{R}#2")]


def test_overlapping_polls_and_restarts_publish_nothing_twice(project, gh, make, clock):
    lifecycle(gh)
    gh.open(9, "dave", T0 - 500, branch="agent/nine")
    gh.push(9, "dave", T0 - 100)
    b = make()
    once(b)
    first = bus(project)
    clock.t += 60
    b.round()
    clock.t += 60
    once(make())                                                      # a restart resumes from the bus
    again = bus(project)
    assert [e for e in again if e["source"] != MACHINE] == [e for e in first if e["source"] != MACHINE]
    assert len({e["id"] for e in again}) == len(again)


def test_a_new_head_after_a_restart_is_a_push_and_the_old_one_is_not(project, gh, make, clock):
    gh.open(5, "alice", T0 - 300, branch="agent/five", committer="alice")
    once(make())
    gh.push(5, "alice", T0 + 100, linked=False)                      # GitHub does not tie the commit to her
    clock.t += 200
    once(make())
    [p] = [e for e in pr_events(project) if e["type"] == "github.pr.pushed"]
    assert p["source"] == "person.alice" and p["data"]["pusher_from"] == "pull_author"
    assert p["data"]["previous"] != p["data"]["head"] == gh.head(5)
    gh.push(5, "erin", T0 + 300, force=True)
    clock.t += 200
    once(make())
    forced = [e for e in pr_events(project) if e["type"] == "github.pr.pushed"][-1]
    assert (forced["source"], forced["data"]["forced"], forced["data"]["pusher_from"]) == ("person.erin", True, "force_push")
    assert forced["time"] == "2026-10-26T07:38:20+00:00"            # GitHub's record of the force push


def test_after_a_gap_it_catches_up_once_with_the_events_own_times(project, gh, make, clock):
    gh.open(6, "alice", T0 - 300, branch="agent/six")
    b = make()
    once(b)
    gh.review(6, "bob", T0 + 1800, "CHANGES_REQUESTED", comments=(("x.py", 3),))    # while the laptop slept
    gh.label(6, "SAIL", "alice", T0 + 3600)
    clock.t += 7200
    b.round()
    [gap] = [e for e in bus(project) if e["type"] == "bridge.gap"]
    assert gap["source"] == MACHINE and gap["data"]["recovered"] is True and gap["data"]["repository"] == R
    late = [e for e in pr_events(project) if e["type"] != "github.pr.opened"]
    assert [(e["type"], e["time"]) for e in late] == [
        ("github.pr.review_submitted", "2026-10-26T08:03:20+00:00"), ("github.pr.review_commented", "2026-10-26T08:03:20+00:00"),
        ("github.pr.labelled", "2026-10-26T08:33:20+00:00")]
    before = bus(project)
    clock.t += 7200                                                   # a second gap, after a restart, finds nothing new
    once(make())
    assert [e["id"] for e in pr_events(project)] == [e["id"] for e in before if e["type"].startswith("github.pr.")]


def test_a_gap_longer_than_max_catchup_is_not_recovered(project, gh, make, clock):
    gh.open(6, "alice", T0 - 300, branch="agent/six")
    b = make()
    once(b)
    gh.label(6, "too-early", "alice", T0 + 3600)                     # beyond the 6 h it will catch up
    gh.label(6, "in-time", "alice", T0 + 9 * 3600)
    clock.t += 10 * 3600
    b.round()
    assert next(e for e in bus(project) if e["type"] == "bridge.gap")["data"]["recovered"] is False
    assert [e["data"]["label"] for e in pr_events(project) if e["type"] == "github.pr.labelled"] == ["in-time"]


def test_a_restart_finds_its_last_good_poll_in_the_store_and_records_the_gap(project, gh, make, clock):
    gh.open(6, "alice", T0 - 300, branch="agent/six")
    once(make())
    store = ProjectStore(for_project(project), project.key)        # what the instrument stored while it ran
    store.write([Point(LAST_POLL, T0, T0, {"instrument": INSTRUMENT, "repo": R})])
    clock.t += 3 * 3600
    once(make())
    [gap] = [e for e in bus(project) if e["type"] == "bridge.gap"]
    assert gap["data"]["from"] == "2026-10-26T07:33:20Z"


def test_a_poll_that_fails_part_way_publishes_nothing(project, gh, make, clock):
    gh.open(6, "alice", T0 - 300, branch="agent/six")
    gh.open(8, "bob", T0 - 300, branch="agent/eight")
    b = make()
    once(b)
    n = len(pr_events(project))
    gh.label(6, "x", "alice", T0 + 10)
    gh.label(8, "y", "bob", T0 + 10)
    gh.fail_next = [500]                                              # the API fails mid-poll
    clock.t += 60
    b.round()
    assert len(pr_events(project)) == n
    clock.t += 60
    b.round()
    assert len(pr_events(project)) == n + 2


def test_people_are_announced_once_and_the_map_names_them(project, gh, make):
    (project.repo_root / "people.toml").write_text('[people.al]\nname = "Alice L"\nteam = "web"\ngithub = "alice"\n')
    gh.open(6, "alice", T0 - 300, branch="agent/six")
    gh.check(6, "build", "success", T0 - 100)
    once(make())
    started = [e for e in bus(project) if e["type"] == "orchestra.machine.started"]
    assert [e["source"] for e in started] == [MACHINE, "person.al", "bot.github-actions"]
    assert started[1]["data"]["team"] == "web" and started[2]["data"]["kind"] == "program"
    assert pr_events(project)[0]["source"] == "person.al"


def test_the_levels_say_what_waits_on_whom(project, gh, make, clock):
    gh.open(1, "alice", T0 - 3600, branch="agent/one")
    gh.request_review(1, "alice", "bob", T0 - 1800)                  # awaiting bob for half an hour
    gh.open(2, "alice", T0 - 3000, branch="agent/two")
    gh.review(2, "bob", T0 - 2000, "CHANGES_REQUESTED")             # awaiting alice's changes
    gh.open(3, "alice", T0 - 3000, branch="agent/three", labels=("SAIL",))   # awaiting SAIL's result
    gh.review(3, "bob", T0 - 2000, "APPROVED")
    gh.check(3, "build", "failure", T0 - 100)
    gh.open(4, "alice", T0 - 3000, branch="agent/four", labels=("SAIL", "SAIL-passed"))
    gh.open(5, "alice", T0 - 3000, branch="agent/five")
    gh.review(5, "bob", T0 - 2000, "CHANGES_REQUESTED")
    gh.push(5, "alice", T0 - 1000)                                   # changes pushed: no longer awaiting them
    b = make()
    assert b.render() is None
    once(b)
    lv = b.measured[R][0]
    assert lv == {"github_prs_open": 5, "github_prs_awaiting_review": 1, "github_prs_awaiting_changes": 1,
                  "github_prs_approved": 1, "github_prs_awaiting_label_result": 1, "github_prs_checks_failing": 1,
                  "github_prs_review_request_oldest_seconds": 1800.0}
    text = b.render()
    assert 'github_prs_awaiting_changes{repo="acme/service"} 1\n' in text
    assert f'github_prs_bridge_last_poll_timestamp_seconds{{repo="acme/service"}} {int(T0)}\n' in text
    gh.merge(2, "alice", T0 + 30)
    clock.t += 60
    b.round()
    assert b.measured[R][0]["github_prs_open"] == 4 and b.measured[R][0]["github_prs_awaiting_changes"] == 0


def test_it_serves_metrics_while_it_runs(project, gh, make):
    gh.open(1, "alice", T0 - 300, branch="agent/one")
    b = make()
    stop = threading.Event()
    t = threading.Thread(target=b.run, args=(stop,), daemon=True)
    t.start()
    try:
        for _ in range(100):
            if b.server and b.measured:
                break
            stop.wait(0.05)
        with urllib.request.urlopen(b.server.url, timeout=5) as r:
            assert 'github_prs_open{repo="acme/service"} 1' in r.read().decode()
    finally:
        stop.set()
        t.join(timeout=10)
