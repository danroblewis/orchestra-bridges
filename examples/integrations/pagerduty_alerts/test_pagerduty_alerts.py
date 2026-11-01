"""pagerduty_alerts against the kit's PagerDuty fake: two intervals of the running poller relay each
incident's change once, as whoever made it; a restart publishes nothing again; ``measure`` prints
the open incidents by urgency and the incidents that opened and closed; it reads only a fake until
cleared; no title, name or token leaves.

Run with Orchestra's own Python (the one ``orchestra`` runs in, which has the kits): ``python -m pytest examples/integrations/pagerduty_alerts/test_pagerduty_alerts.py``."""

import importlib.util
import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

from orchestra_bridges.kits.fakes import CANARY, FakePagerDuty
from orchestra_bridges.kits.testing import fake_token

HERE = Path(__file__).resolve().parent
PROGRAM = HERE / "pagerduty_alerts.py"
TOKEN = fake_token(HERE)        # the placeholder the runner sends a fake on this machine
REAL = "real-token-never-sent-to-a-fake"   # in the environment, yet never read for a fake
STARTED = "orchestra.machine.started"


def program():
    spec = importlib.util.spec_from_file_location("pagerduty_alerts", PROGRAM)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def env(tmp, **extra):
    e = {k: v for k, v in os.environ.items() if k != "PAGERDUTY_TOKEN"}
    return {**e, "ORCHESTRATOR_HOME": str(tmp / "home"), **extra}


def common(fake, tmp):
    return ["--base-url", fake.url, "--state-dir", str(tmp / "state"), "--ttl", "0"]


def events_of(path: Path) -> list[dict]:
    return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


def relayed(events):
    return [(e["type"], e["subject"], e["source"]) for e in events if e["type"] != STARTED]


def wait_for(path: Path, n: int, timeout: float = 30.0) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if len(relayed(events_of(path))) >= n:
            return
        time.sleep(0.05)
    raise AssertionError(f"fewer than {n} events: {events_of(path)}")


def run_two_intervals(fake, tmp, change, **extra_env):
    """The poller as a process, every 2 s for two polls; ``change`` alters the world between them."""
    ev = tmp / "events.jsonl"
    proc = subprocess.Popen([sys.executable, str(PROGRAM), "--every", "2", "--rounds", "2", "--events-file", str(ev),
                             *common(fake, tmp)], env=env(tmp, **extra_env), stderr=subprocess.PIPE, text=True)
    try:
        wait_for(ev, 2)
        change(fake)
        assert proc.wait(timeout=30) == 0
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()
    return events_of(ev), proc.stderr.read()


def change(fake):
    fake.set_status("PINC001", "resolved", by="PUSER02")
    fake.add_incident("PINC003", service="PSVC002", urgency="low")


@pytest.fixture
def fake():
    with FakePagerDuty() as f:                          # the fake needs no token
        yield f


def test_two_intervals_relay_each_change_once_and_a_restart_adds_nothing(fake, tmp_path):
    events, log = run_two_intervals(fake, tmp_path, change)
    got = relayed(events)
    assert got[:2] == [("pagerduty.incident.triggered", "PINC001", "bot.pagerduty"),
                       ("pagerduty.incident.acknowledged", "PINC002", "person.pagerduty.puser01")]
    assert sorted(got[2:]) == [("pagerduty.incident.resolved", "PINC001", "person.pagerduty.puser02"),
                               ("pagerduty.incident.triggered", "PINC003", "bot.pagerduty")]
    announced = {e["source"]: e["data"] for e in events if e["type"] == STARTED}
    assert sorted(announced) == ["bot.pagerduty", "person.pagerduty.puser01", "person.pagerduty.puser02"]
    assert announced["bot.pagerduty"]["kind"] == "program" and announced["person.pagerduty.puser01"]["kind"] == "person"
    assert len([e for e in events if e["type"] == STARTED]) == 3                       # each actor once
    assert not any(e["source"] == "pagerduty_alerts" for e in events)                 # a relay never publishes as itself
    assert "poll 2" in log and "stopped" in log

    rc = program().main(["--once", "--events-file", str(tmp_path / "events.jsonl"), *common(fake, tmp_path)])
    assert rc == 0 and events_of(tmp_path / "events.jsonl") == events                 # a restart: nothing twice


def test_measure_prints_the_open_incidents_and_their_flows(fake, tmp_path, capsys):
    run_two_intervals(fake, tmp_path, change)
    assert program().main(["measure", *common(fake, tmp_path)]) == 0
    out = capsys.readouterr().out
    assert 'pagerduty_incidents_open{urgency="high"} 0' in out
    assert 'pagerduty_incidents_open{urgency="low"} 2' in out
    assert 'pagerduty_incidents_triggered_total{urgency="high"} 1' in out
    assert 'pagerduty_incidents_triggered_total{urgency="low"} 2' in out
    assert 'pagerduty_incidents_resolved_total{urgency="high"} 1' in out
    assert "# TYPE pagerduty_incidents_open gauge" in out


def test_no_text_or_token_leaves(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("PAGERDUTY_TOKEN", REAL)
    monkeypatch.setenv("ORCHESTRATOR_HOME", str(tmp_path / "home"))
    with FakePagerDuty(token=TOKEN) as f:
        _, log = run_two_intervals(f, tmp_path, change, PAGERDUTY_TOKEN=REAL)
        assert program().main(["measure", *common(f, tmp_path)]) == 0
        assert f.requests and all(r.authorized for r in f.requests)
    out = capsys.readouterr()
    for text in (out.out, out.err, log):
        assert CANARY not in text and REAL not in text
    for p in tmp_path.rglob("*"):
        if p.is_file():
            text = p.read_text(errors="replace")
            assert CANARY not in text and REAL not in text, p


def test_reads_only_a_fake_until_cleared(tmp_path, monkeypatch):
    monkeypatch.setenv("PAGERDUTY_TOKEN", REAL)
    rc = program().main(["--once", "--base-url", "https://api.pagerduty.com", "--state-dir", str(tmp_path / "s"),
                         "--events-file", str(tmp_path / "e.jsonl")])
    assert rc == 2 and not (tmp_path / "e.jsonl").exists()
