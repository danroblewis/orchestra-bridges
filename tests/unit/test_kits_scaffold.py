"""The scaffold: ``orchestra bridges scaffold <kind> <name> [--root]`` writes a poller from a kit
into the chosen root; each kind's own test passes against its fake; a scaffolded PagerDuty poller
runs for two intervals as a process, relays two incidents' changes and stops on SIGTERM, and its
``measure`` prints the open-incidents gauge. Also ``[bridges.kits]`` and its check."""

import json
import os
import signal
import subprocess
import sys
import time
import tomllib

import pytest
from conftest import checkout_env, new_project

from orchestra_bridges.__main__ import main as bridges_main
from orchestra_bridges.check import check
from orchestra_bridges.kits.fakes import CANARY, FakePagerDuty
from orchestra_bridges.kits.machinefile import MachineFileError, read, require_clearance
from orchestra_bridges.kits.scaffold import KINDS, ScaffoldError, root_dir, scaffold
from orchestra_bridges.kits.settings import read as read_settings

TOKEN = "scaffold-test-token-not-real"


@pytest.fixture
def proj(tmp_path, monkeypatch):
    return new_project(tmp_path, monkeypatch, "")


def test_writes_the_four_files_into_the_state_root(proj, capsys):
    assert bridges_main(["scaffold", "pagerduty", "pd_alerts"]) == 0
    d = proj.state_dir / "machines" / "pd_alerts"
    assert sorted(p.name for p in d.iterdir()) == ["AGENTS.md", "machine.toml", "pd_alerts.py", "test_pd_alerts.py"]
    toml = tomllib.loads((d / "machine.toml").read_text())
    assert toml["name"] == "pd_alerts" and toml["clearance"] == "needs approval"
    assert toml["settings"]["token_env"] == "PAGERDUTY_TOKEN" and "token" not in toml["settings"]
    agents = (d / "AGENTS.md").read_text()
    assert "{{" not in agents and "$PAGERDUTY_TOKEN" in agents and "[[collect]]" in agents and "measure" in agents
    assert "never a machine" in agents
    out = capsys.readouterr().out
    assert "[[machines]]" in out and "pd_alerts.py measure" in out and 'restart = "always"' in out


def test_roots(proj):
    assert root_dir(proj, "state") == proj.state_dir
    assert root_dir(proj, "repo") == proj.repo_root / ".orchestra"
    assert root_dir(proj, "overlay") == proj.overlay_file.parent
    assert bridges_main(["scaffold", "jira", "jira_poll", "--root", "repo"]) == 0
    assert (proj.repo_root / ".orchestra" / "machines" / "jira_poll" / "jira_poll.py").is_file()


def test_refuses_a_bad_name_an_unknown_kind_and_an_existing_directory(proj, tmp_path):
    with pytest.raises(ScaffoldError):
        scaffold("pagerduty", "Bad-Name", tmp_path)
    with pytest.raises(ScaffoldError):
        scaffold("nagios", "ok_name", tmp_path)
    scaffold("slack", "chat", tmp_path)
    with pytest.raises(ScaffoldError, match="exists"):
        scaffold("slack", "chat", tmp_path)


@pytest.mark.parametrize("kind", sorted(KINDS))
def test_each_kinds_own_test_passes_against_its_fake(kind, tmp_path):
    d = scaffold(kind, f"my_{kind}", tmp_path / "root")
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(d / f"test_my_{kind}.py")],
                       capture_output=True, text=True, timeout=300, cwd=tmp_path, env=checkout_env(), check=False)
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-2000:]


def events_of(path):
    return [json.loads(x) for x in path.read_text().splitlines()] if path.exists() else []


def test_a_scaffolded_pagerduty_poller_runs_two_intervals_and_stops(tmp_path, monkeypatch):
    d = scaffold("pagerduty", "pd_alerts", tmp_path / "root")
    ev = tmp_path / "events.jsonl"
    env = checkout_env(PAGERDUTY_TOKEN=TOKEN, ORCHESTRATOR_HOME=str(tmp_path / "home"))
    common = ["--base-url", None, "--state-dir", str(tmp_path / "state"), "--ttl", "0"]
    with FakePagerDuty(token=TOKEN) as fake:
        common[1] = fake.url
        proc = subprocess.Popen([sys.executable, str(d / "pd_alerts.py"), "--every", "1", "--events-file", str(ev),
                                 *common], env=env, stderr=subprocess.PIPE, text=True)
        try:
            def wait_for(n):
                deadline = time.time() + 30
                while time.time() < deadline:
                    got = [e for e in events_of(ev) if e["type"].startswith("pagerduty.incident.")]
                    if len(got) >= n:
                        return got
                    time.sleep(0.05)
                raise AssertionError(f"fewer than {n} events: {events_of(ev)}")
            first = wait_for(2)                              # interval 1: both open incidents
            fake.set_status("PINC001", "resolved", by="PUSER02")
            got = wait_for(3)                                # interval 2: the resolution, and nothing twice
            time.sleep(1.5)
            proc.send_signal(signal.SIGTERM)
            assert proc.wait(timeout=15) == 0
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        log = proc.stderr.read()
        measured = subprocess.run([sys.executable, str(d / "pd_alerts.py"), "measure", *common], env=env,
                                  capture_output=True, text=True, timeout=60, check=False)
    assert [(e["type"], e["subject"]) for e in first[:2]] == [("pagerduty.incident.triggered", "PINC001"),
                                                              ("pagerduty.incident.acknowledged", "PINC002")]
    got = [e for e in events_of(ev) if e["type"].startswith("pagerduty.incident.")]
    assert len(got) == 3 and (got[2]["type"], got[2]["source"]) == ("pagerduty.incident.resolved",
                                                                    "person.pagerduty.puser02")
    assert len({e["id"] for e in got}) == 3
    assert {e["source"] for e in events_of(ev) if e["type"] == "orchestra.machine.started"} == {
        "bot.pagerduty", "person.pagerduty.puser01", "person.pagerduty.puser02"}
    assert "poll 2" in log and "stopped" in log
    assert measured.returncode == 0 and "pagerduty_incidents_open_total 1" in measured.stdout
    for p in tmp_path.rglob("*"):
        if p.is_file() and p.suffix in (".json", ".jsonl", ".log", ".prom"):
            text = p.read_text(errors="replace")
            assert CANARY not in text and TOKEN not in text, p
    assert CANARY not in log + measured.stdout + measured.stderr and TOKEN not in log


def test_clearance(tmp_path):
    d = scaffold("pagerduty", "pd_alerts", tmp_path)
    m = read(d / "machine.toml")
    require_clearance(m, "http://127.0.0.1:9999")                  # a fake: always
    with pytest.raises(MachineFileError, match="not cleared"):
        require_clearance(m, "https://api.pagerduty.com")
    (d / "machine.toml").write_text((d / "machine.toml").read_text().replace('"needs approval"', '"approved"'))
    require_clearance(read(d / "machine.toml"), "https://api.pagerduty.com")
    (d / "machine.toml").write_text((d / "machine.toml").read_text().replace("\n[settings]\n", '\n[settings]\ntoken = "x"\n'))
    with pytest.raises(MachineFileError, match="token"):
        read(d / "machine.toml")


def test_kits_settings_and_check():
    raw = {"bridges": {"kits": {"cache_dir": "~/cache", "ttl": "2m", "per_minute": 30,
                                "hosts": {"api.pagerduty.com": 10}, "tokens": {"pagerduty": "PAGERDUTY_TOKEN"}}}}
    s = read_settings(raw)
    assert s.ttl_s == 120 and s.per_minute_for("API.pagerduty.com") == 10 and s.per_minute_for("x") == 30
    assert s.token_env("pagerduty") == "PAGERDUTY_TOKEN" and str(s.cache_dir).startswith(os.path.expanduser("~"))
    assert check(raw) == []
    pasted = "u+abcdefghijklmnop"
    bad = check({"bridges": {"kits": {"tokens": {"pagerduty": pasted}}}})
    assert bad and pasted not in bad[0]                             # never echoes what may be a token
    assert check({"bridges": {"kits": {"token": "x"}}})
    assert check({"bridges": {"kits": {"per_minute": 0}}})
    assert check({"bridges": {"kits": {"nope": 1}}})
    assert check({"bridges": {"kits": {"hosts": {"a b": 1}}}})


def _measure(d, fake, tmp_path):
    return subprocess.run([sys.executable, str(d / "pd_alerts.py"), "measure", "--base-url", fake.url, "--state-dir",
                           str(tmp_path / "state"), "--ttl", "0"], capture_output=True, text=True, timeout=60,
                          env=checkout_env(ORCHESTRATOR_HOME=str(tmp_path / "home")), check=False)


def test_the_runner_reads_the_token_from_token_file(tmp_path, monkeypatch):
    monkeypatch.delenv("PAGERDUTY_TOKEN", raising=False)
    d = scaffold("pagerduty", "pd_alerts", tmp_path / "root")
    secret = tmp_path / "secrets" / "pd-token"
    secret.parent.mkdir()
    secret.write_text(TOKEN + "\n")
    secret.chmod(0o600)
    toml = (d / "machine.toml").read_text().replace("\n[settings]\n", f'\n[settings]\ntoken_file = "{secret}"\n')
    (d / "machine.toml").write_text(toml)
    assert read(d / "machine.toml").token_file == str(secret)
    with FakePagerDuty(token=TOKEN) as fake:
        r = _measure(d, fake, tmp_path)
        assert r.returncode == 0, r.stderr
        assert fake.requests and all(q.authorized for q in fake.requests)
    assert TOKEN not in r.stdout + r.stderr


def test_a_fake_needs_no_token(tmp_path, monkeypatch):
    monkeypatch.delenv("PAGERDUTY_TOKEN", raising=False)
    d = scaffold("pagerduty", "pd_alerts", tmp_path / "root")
    with FakePagerDuty() as fake:                     # a fake that checks no token
        r = _measure(d, fake, tmp_path)
    assert r.returncode == 0 and "pagerduty_incidents_open_total 2" in r.stdout, r.stderr
