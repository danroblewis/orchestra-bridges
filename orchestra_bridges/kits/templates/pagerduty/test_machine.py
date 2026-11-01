"""{{name}} against the kit's PagerDuty fake: two polls (a restart between them) relay each
incident's change once, as whoever made it; ``measure`` prints the open gauge; no title, name or
token leaves the program.

Run: ``uv run python -m pytest {{dir}}/test_{{name}}.py`` (in an environment with orchestra-bridges)."""

import importlib.util
import json
from pathlib import Path

import pytest

from orchestra_bridges.kits.fakes import CANARY, FakePagerDuty

HERE = Path(__file__).resolve().parent
TOKEN = "test-token-not-real"


def program():
    spec = importlib.util.spec_from_file_location("{{name}}", HERE / "{{name}}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def fake(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCHESTRATOR_HOME", str(tmp_path / "home"))     # the rate bucket's file goes here
    monkeypatch.setenv("PAGERDUTY_TOKEN", TOKEN)
    with FakePagerDuty(token=TOKEN) as f:
        yield f


def args(fake, tmp, *extra):
    return [*extra, "--base-url", fake.url, "--state-dir", str(tmp / "state"), "--ttl", "0"]


def poll_once(fake, tmp):
    assert program().main(args(fake, tmp, "--once", "--events-file", str(tmp / "events.jsonl"))) == 0
    return [json.loads(x) for x in (tmp / "events.jsonl").read_text().splitlines()]


def changes(events):
    return [(e["type"], e["subject"], e["source"]) for e in events if e["type"] != "orchestra.machine.started"]


def test_two_polls_relay_each_change_once(fake, tmp_path):
    events = poll_once(fake, tmp_path)
    assert changes(events) == [("pagerduty.incident.triggered", "PINC001", "bot.pagerduty"),
                               ("pagerduty.incident.acknowledged", "PINC002", "person.pagerduty.puser01")]
    announced = {e["source"]: e["data"] for e in events if e["type"] == "orchestra.machine.started"}
    assert announced["bot.pagerduty"]["kind"] == "program" and announced["person.pagerduty.puser01"]["kind"] == "person"
    assert not any(e["source"] == "{{name}}" for e in events)          # a relay never publishes as itself

    fake.set_status("PINC001", "resolved", by="PUSER02")
    events = poll_once(fake, tmp_path)                                  # a restart: the cursor is read back
    assert changes(events)[2:] == [("pagerduty.incident.resolved", "PINC001", "person.pagerduty.puser02")]
    assert len([e for e in events if e["type"] == "orchestra.machine.started"]) == 3   # each actor once
    assert all(r.authorized for r in fake.requests)


def test_measure_prints_the_open_incidents(fake, tmp_path, capsys):
    assert program().main(args(fake, tmp_path, "measure")) == 0
    out = capsys.readouterr().out
    assert "pagerduty_incidents_open_total" not in out
    assert 'pagerduty_incidents_open{service="PSVC001",urgency="high"} 1' in out


def test_no_text_or_token_leaves(fake, tmp_path, capsys):
    poll_once(fake, tmp_path)
    program().main(args(fake, tmp_path, "measure"))
    out = capsys.readouterr()
    assert CANARY not in out.out + out.err and TOKEN not in out.out + out.err
    for p in tmp_path.rglob("*"):
        if p.is_file():
            text = p.read_text(errors="replace")
            assert CANARY not in text and TOKEN not in text, p


def test_refuses_an_uncleared_system(tmp_path, monkeypatch):
    monkeypatch.setenv("PAGERDUTY_TOKEN", TOKEN)
    rc = program().main(["--once", "--base-url", "https://api.pagerduty.com", "--state-dir", str(tmp_path / "s"),
                         "--events-file", str(tmp_path / "e.jsonl")])
    assert rc == 2 and not (tmp_path / "e.jsonl").exists()
