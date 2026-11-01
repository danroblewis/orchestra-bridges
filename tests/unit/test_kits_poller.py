"""The kits' Poller: the relay rule (sources are actors; each announced once; never itself), the
cursor persisted, emission idempotent across a restart, a failed poll saving nothing, the series
file, and a clean stop on SIGTERM."""

import json
import os
import signal
import subprocess
import sys
import textwrap
import time

import pytest
from orchestra.events import Events

from orchestra_bridges.kits.exposition import Registry, show
from orchestra_bridges.kits.exposition import main as show_main
from orchestra_bridges.kits.poller import Poller, RelayError, actor_source


def make(tmp_path, **kw):
    return Poller("demo_poller", every_s=0.05, state_dir=tmp_path / "state", events=Events(tmp_path / "ev.jsonl"),
                  log=lambda m: None, **kw)


def read(tmp_path):
    p = tmp_path / "ev.jsonl"
    return [json.loads(x) for x in p.read_text().splitlines()] if p.exists() else []


def items_poll(items):
    def poll(p):
        for key, status, who in items:
            p.emit(f"demo.item.{status}", key, {"status": status}, source=actor_source("demo", who), stamp=status)
        p.cursor = len(items)
    return poll


def test_actor_source():
    assert actor_source("pagerduty", "PUSER01") == "person.pagerduty.puser01"
    assert actor_source("slack", "B001", bot=True) == "bot.slack.b001"
    assert actor_source("jira", None) == "bot.jira"
    assert actor_source("x", "a b/c") == "person.x.a-b-c"


def test_relays_as_actors_and_announces_each_once(tmp_path):
    p = make(tmp_path)
    p.run(items_poll([("A-1", "opened", "ann"), ("A-2", "opened", "ann"), ("A-1", "closed", None)]), once=True)
    evs = read(tmp_path)
    started = [e for e in evs if e["type"] == "orchestra.machine.started"]
    assert [e["source"] for e in started] == ["person.demo.ann", "bot.demo"]
    assert started[0]["data"] == {"mode": "ad hoc", "kind": "person", "listens_for": [], "package": "demo_poller"}
    assert started[1]["data"]["kind"] == "program"
    assert not any(e["source"] == "demo_poller" for e in evs)
    first = evs.index(started[0])
    assert evs[first + 1]["subject"] == "A-1"                              # announced just before the first event


def test_notes_go_on_each_actors_announcement_and_a_change_announces_again(tmp_path):
    """K7: a poller's notes (relayed from a fake, a stand-in, disabled) are in each actor's own
    announcement, so they show with that machine; a changed note announces each actor again, once."""
    p = make(tmp_path, notes={"fake": "relayed from a fake", "disabled": ""})
    p.run(items_poll([("A-1", "opened", "ann")]), once=True)
    q = make(tmp_path, notes={"fake": "relayed from a fake"})          # a restart, the same notes: nothing again
    q.run(items_poll([("A-1", "opened", "ann"), ("A-2", "opened", "ann")]), once=True)
    r = make(tmp_path, notes={"stand_in": "reviews as comments"})       # the notes changed: announced again
    r.run(items_poll([("A-3", "opened", "ann")]), once=True)
    started = [e["data"] for e in read(tmp_path) if e["type"] == "orchestra.machine.started"]
    assert len(started) == 2
    assert started[0]["fake"] == "relayed from a fake" and "disabled" not in started[0]
    assert started[1]["stand_in"] == "reviews as comments" and "fake" not in started[1]
    with pytest.raises(ValueError, match="no note"):
        make(tmp_path, notes={"title": "x"})
    with pytest.raises(ValueError, match="one line"):
        make(tmp_path, notes={"disabled": "two\nlines"})


def test_refuses_any_source_but_an_actor(tmp_path):
    p = make(tmp_path)
    for bad in (None, "demo_poller", "orchestra-host", "bridges.demo"):
        with pytest.raises(RelayError):
            p.emit("demo.item.opened", "A-1", source=bad)
    with pytest.raises(RelayError):
        p.emit("orchestra.machine.started", "x", source="person.demo.ann")


def test_idempotent_across_a_restart_and_cursor_persisted(tmp_path):
    items = [("A-1", "opened", "ann"), ("A-2", "opened", "bob")]
    make(tmp_path).run(items_poll(items), once=True)
    assert len(read(tmp_path)) == 4                                        # two announcements, two events
    again = make(tmp_path)                                                 # a restart
    assert again.cursor == 2
    again.run(items_poll([*items, ("A-1", "closed", "ann")]), once=True)
    evs = read(tmp_path)
    assert [e["type"] for e in evs[4:]] == ["demo.item.closed"]            # nothing twice; ann not re-announced
    assert again.cursor == 3
    assert json.loads((tmp_path / "state" / "cursor.json").read_text())["cursor"] == 3


def test_event_ids_are_the_items_and_stamps(tmp_path):
    p = make(tmp_path)
    ev = p.emit("demo.item.opened", "A-1", {"n": 1}, source="person.demo.ann", stamp="2026-10-31T08:00:00Z")
    assert ev["id"] == "demo_poller:demo.item.opened:A-1@2026-10-31T08:00:00Z"
    assert p.emit("demo.item.opened", "A-1", {"n": 1}, source="person.demo.ann", stamp="2026-10-31T08:00:00Z") is None
    assert p.emit("demo.item.opened", "A-1", {"n": 2}, source="person.demo.ann") is not None   # data hash as stamp


def test_a_failed_poll_saves_nothing(tmp_path):
    p = make(tmp_path)
    p.cursor = "before"

    def bad(poller):
        poller.cursor = "after"
        poller.counter("demo_total", 5)
        raise RuntimeError("the API is down")
    assert p.run(bad, once=True) == 1
    assert p.cursor == "before" and p.failures == 1 and not p.series.counters
    assert not (tmp_path / "state" / "cursor.json").exists()


def test_series_written_after_a_good_poll_and_counters_kept(tmp_path):
    def poll(p):
        p.gauge("demo_open", 3, {"kind": "a"}, help="Open items.")
        p.counter("demo_seen_total", 2)
    make(tmp_path).run(poll, rounds=2)
    text = (tmp_path / "state" / "metrics.prom").read_text()
    assert 'demo_open{kind="a"} 3' in text and "demo_seen_total 4" in text and "# TYPE demo_open gauge" in text
    make(tmp_path).run(poll, once=True)                                    # a restart: the counter goes on
    assert "demo_seen_total 6" in (tmp_path / "state" / "metrics.prom").read_text()


def test_exposition_show_refuses_a_stale_file(tmp_path, capsys):
    r = Registry()
    r.gauge("x", 1.5, {"a": 'q"uote'})
    f = tmp_path / "m.prom"
    r.write(f)
    assert show(f, 60) == '# TYPE x gauge\nx{a="q\\"uote"} 1.5\n'
    assert show(f, 60, now=time.time() + 120) is None
    assert show_main([str(f), "--max-age", "60"]) == 0 and "x{" in capsys.readouterr().out
    os.utime(f, (time.time() - 3600, time.time() - 3600))
    assert show_main([str(f), "--max-age", "60"]) == 1


def test_registry_refuses_bad_names():
    r = Registry()
    with pytest.raises(ValueError):
        r.gauge("bad-name", 1)
    with pytest.raises(ValueError):
        r.counter("ok_total", -1)


def test_stops_cleanly_on_sigterm(tmp_path):
    script = tmp_path / "p.py"
    script.write_text(textwrap.dedent(f"""
        from pathlib import Path
        from orchestra.events import Events
        from orchestra_bridges.kits.poller import Poller
        def poll(p):
            p.emit("demo.tick.seen", str(p.polls), source="bot.demo")
        Poller("demo_poller", every_s=0.1, state_dir=Path({str(tmp_path / 's')!r}),
               events=Events(Path({str(tmp_path / 'ev.jsonl')!r}))).run(poll)
    """))
    proc = subprocess.Popen([sys.executable, str(script)], stderr=subprocess.PIPE, text=True)
    try:
        deadline = time.time() + 20
        while time.time() < deadline and len(read(tmp_path)) < 3:
            time.sleep(0.05)
        proc.send_signal(signal.SIGTERM)
        assert proc.wait(timeout=10) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
    assert "stopped" in proc.stderr.read()
