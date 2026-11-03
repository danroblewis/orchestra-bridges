"""The scripted team (``orchestra_bridges.team``) against the kits' fakes: the day's script, a whole
day played, ``--once`` resuming, ``--dry-run`` writing nothing, ``--close`` closing only the team's
tickets with a resolution, the ledger (metadata only), and a write at most every 2 seconds. The
real systems are never called here: every URL is a fake on this machine."""

import itertools
import json

import pytest

from orchestra_bridges.kits.fakes import FakeDiscord, FakeJira
from orchestra_bridges.team.__main__ import main
from orchestra_bridges.team.day import PREFIX, ROUNDS, day
from orchestra_bridges.team.wire import Throttle

WORLD = {"guild": {"id": "1000", "name": "test"}, "users": [], "names": {"C1": "team-chat", "C2": "alerts"},
         "channels": {"C1": [], "C2": []}}


class Clock:
    def __init__(self):
        self.t, self.writes = 1000.0, []

    def clock(self):
        return self.t

    def sleep(self, s):
        self.t += max(0.0, s)


@pytest.fixture
def fakes():
    with FakeJira(flavour="cloud", me="acct-me") as j, FakeDiscord(WORLD) as d:
        yield j, d


def run(j, d, tmp_path, *extra, clock=None):
    clock = clock or Clock()
    argv = ["--jira-url", j.url, "--jira-flavour", "cloud", "--project", "SCRUM", "--discord-url", d.url,
            "--channel", "C1", "--channel", "C2", "--ledger", str(tmp_path / "ledger.json"), "--rhythm", "16m", *extra]
    return main(argv, clock=clock.clock, sleep=clock.sleep)


def writes(fake):
    return [r for r in fake.requests if r.method != "GET"]


def scrum(j):
    return {i["key"]: i for i in j.data["issues"] if i["project"] == "SCRUM"}


def test_the_day_is_drawn_from_its_seed_and_keeps_its_order():
    a, b = day(1, 2), day(2, 2)
    assert a == day(1, 2) and a != b and len(a.rounds) == ROUNDS
    born: set[str] = set()
    for acts in a.rounds:
        for act in acts:
            if act.op in ("create", "post"):
                born.add(act.ref)
            else:
                assert act.ref in born, act                       # a ticket or message exists before it is touched
            assert all(t in born for t in act.refs())
            if act.op == "create":
                assert act.text.startswith(PREFIX)
    ops = [x.op for acts in a.rounds for x in acts]
    assert ops.count("create") == 5 and ops.count("post") == 6 and ops.count("check") == 3
    assert {x.kind for acts in a.rounds for x in acts if x.op == "post"} == {"status", "question", "request", "incident"}


def test_a_whole_day_against_the_fakes(fakes, tmp_path, capsys):
    j, d = fakes
    clock = Clock()
    assert run(j, d, tmp_path, clock=clock) == 0
    tickets = scrum(j)
    assert len(tickets) == 5
    for i in tickets.values():
        assert PREFIX.strip() in i["summary"] and {"orchestra-live-test", "orchestra-live-team"} <= set(i["labels"])
    ledger = json.loads((tmp_path / "ledger.json").read_text())
    [day_] = ledger["days"]
    assert day_["played"] == ROUNDS and set(day_["tickets"]) == {"t1", "t2", "t3", "t4", "t5"}
    status = {ref: tickets[t["key"]]["status"] for ref, t in day_["tickets"].items()}
    assert status == {"t1": "Done", "t2": "Done", "t3": "In Review", "t4": "In Progress", "t5": "Open"}
    assert all(tickets[t["key"]]["assignee"] == "acct-me" for t in day_["tickets"].values())
    posted = [r for r in writes(d) if r.method == "POST"]
    assert len(posted) == 10 and sum(1 for r in posted if "message_reference" in r.body) == 4
    assert sum(1 for r in writes(d) if r.method == "PUT" and r.path.endswith("/@me")) == 3
    assert {m["channel"] for m in day_["messages"].values()} <= {"C1", "C2"}
    assert not [r for r in j.requests + d.requests if r.method == "DELETE"]
    text = (tmp_path / "ledger.json").read_text()                 # metadata only: no summary, comment or message
    assert not any(a.text and a.text in text for acts in day(1, 2).rounds for a in acts)
    assert clock.t - 1000.0 >= 14 * 60                            # the rhythm: 16m over eight rounds, the last one started
    out = capsys.readouterr().out
    assert "#team-chat" in out or "#alerts" in out


def test_every_write_waits_two_seconds_after_the_last():
    c = Clock()
    t = Throttle(clock=c.clock, sleep=c.sleep)
    times = []
    for _ in range(4):
        t.wait()
        times.append(c.t)
    assert [b - a for a, b in itertools.pairwise(times)] == [2.0, 2.0, 2.0] and t.writes == 4


def test_writes_are_spaced_even_when_the_rhythm_is_fast(fakes, tmp_path, monkeypatch):
    j, d = fakes
    clock, stamps = Clock(), []
    real = Throttle.wait

    def wait(self):
        real(self)
        stamps.append(clock.t)

    monkeypatch.setattr(Throttle, "wait", wait)
    assert run(j, d, tmp_path, "--rhythm", "1s", clock=clock) == 0
    assert len(stamps) > 20 and min(b - a for a, b in itertools.pairwise(stamps)) >= 2.0


def test_once_plays_one_round_and_the_next_run_resumes(fakes, tmp_path):
    j, d = fakes
    assert run(j, d, tmp_path, "--once", "--seed", "7") == 0
    ledger = json.loads((tmp_path / "ledger.json").read_text())
    assert ledger["days"][0]["played"] == 1 and len(scrum(j)) == 3
    assert run(j, d, tmp_path, "--once", "--seed", "99") == 0      # the unfinished day goes on, with its own seed
    ledger = json.loads((tmp_path / "ledger.json").read_text())
    assert len(ledger["days"]) == 1 and ledger["days"][0]["played"] == 2 and ledger["days"][0]["seed"] == 7
    assert len(scrum(j)) == 4


def test_dry_run_writes_nothing_anywhere(fakes, tmp_path, capsys):
    j, d = fakes
    assert run(j, d, tmp_path, "--dry-run") == 0
    assert not writes(j) and not writes(d) and not (tmp_path / "ledger.json").exists()
    out = capsys.readouterr().out
    assert "jira create t1" in out and "(dry run)" in out and "discord post m1" in out and PREFIX.strip() in out


def test_close_closes_only_the_teams_tickets_with_a_resolution_and_deletes_nothing(fakes, tmp_path, capsys):
    j, d = fakes
    assert run(j, d, tmp_path, "--once") == 0
    path = tmp_path / "ledger.json"
    ledger = json.loads(path.read_text())
    ledger["days"][0]["tickets"]["t9"] = {"key": "ABC-1", "id": "10001", "created": "x", "closed": None}   # not the team's
    path.write_text(json.dumps(ledger))
    assert run(j, d, tmp_path, "--close", "--dry-run") == 0
    assert all(i["status"] == "Open" for i in scrum(j).values())
    assert run(j, d, tmp_path, "--close") == 0
    for i in scrum(j).values():
        assert i["status"] == "Done" and i["resolution"] == "Done"
    assert next(i for i in j.data["issues"] if i["key"] == "ABC-1")["status"] == "Open"
    closed = json.loads(path.read_text())["days"][0]["tickets"]
    assert all(closed[r]["closed"]["resolution"] == "Done" for r in ("t1", "t2", "t3"))
    assert closed["t9"]["closed"] is None
    assert "ABC-1: refused" in capsys.readouterr().out
    assert not [r for r in j.requests + d.requests if r.method == "DELETE"]
    n = len(writes(j))
    assert run(j, d, tmp_path, "--close") == 0 and len(writes(j)) == n   # nothing left to close


def test_a_missing_credential_refuses_before_any_write(tmp_path, capsys):
    rc = main(["--jira-url", "https://example.atlassian.net", "--jira-token-file", str(tmp_path / "none"),
               "--ledger", str(tmp_path / "l.json")])
    assert rc == 2 and "no token file" in capsys.readouterr().err and not (tmp_path / "l.json").exists()


def test_nothing_to_play_against_is_refused(tmp_path):
    assert main(["--ledger", str(tmp_path / "l.json")]) == 2
