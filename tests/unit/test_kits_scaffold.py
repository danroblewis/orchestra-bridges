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
from orchestra_bridges.kits.runner import FAKE_TOKEN, FAKE_USER, credentials
from orchestra_bridges.kits.scaffold import KINDS, TEMPLATES, ScaffoldError, root_dir, scaffold
from orchestra_bridges.kits.settings import read as read_settings

TOKEN = FAKE_TOKEN                       # what the runner sends a fake on this machine
REAL = "real-scaffold-token-never-sent-to-a-fake"


@pytest.fixture
def proj(tmp_path, monkeypatch):
    return new_project(tmp_path, monkeypatch, "")


def test_writes_the_four_files_into_the_repository_by_default(proj, capsys):
    assert bridges_main(["scaffold", "pagerduty", "pd_alerts"]) == 0
    d = proj.repo_root / ".orchestra" / "machines" / "pd_alerts"
    assert sorted(p.name for p in d.iterdir()) == ["AGENTS.md", "pd_alerts.py", "poller.toml", "test_pd_alerts.py"]
    toml = tomllib.loads((d / "poller.toml").read_text())
    assert toml["name"] == "pd_alerts" and toml["clearance"] == "needs approval"
    assert toml["settings"]["token_env"] == "PAGERDUTY_TOKEN" and "token" not in toml["settings"]
    agents = (d / "AGENTS.md").read_text()
    assert "{{" not in agents and "$PAGERDUTY_TOKEN" in agents and "[[collect]]" in agents and "measure" in agents
    assert "never a machine" in agents and "uv run python" not in agents
    out = capsys.readouterr().out
    # the test command with Orchestra's own Python (the repository's environment may not have the kits)
    assert f"{sys.executable} -m pytest {d}/test_pd_alerts.py -q" in out
    # the entries name the script from the repository's root, where the launcher runs commands
    assert 'command = "python .orchestra/machines/pd_alerts/pd_alerts.py"' in out
    assert 'command = "python .orchestra/machines/pd_alerts/pd_alerts.py measure"' in out
    assert 'restart = "always"' in out and 'every = "5m"' in out and 'timeout = "60s"' in out   # INTEGRATING.md's


def test_the_state_root_names_the_script_by_its_full_path(proj, capsys):
    assert bridges_main(["scaffold", "pagerduty", "pd_alerts", "--root", "state"]) == 0
    d = proj.state_dir / "machines" / "pd_alerts"
    assert (d / "pd_alerts.py").is_file()
    assert f'command = "python {d}/pd_alerts.py measure"' in capsys.readouterr().out


def test_roots(proj):
    assert root_dir(proj, "state") == proj.state_dir
    assert root_dir(proj, "repo") == proj.repo_root / ".orchestra"
    assert root_dir(proj, "overlay") == proj.overlay_file.parent
    assert bridges_main(["scaffold", "jira", "jira_poll", "--root", "overlay"]) == 0
    assert (proj.overlay_file.parent / "machines" / "jira_poll" / "jira_poll.py").is_file()


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
    env = checkout_env(PAGERDUTY_TOKEN=REAL, ORCHESTRATOR_HOME=str(tmp_path / "home"))
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
    assert measured.returncode == 0 and 'pagerduty_incidents_open{service="PSVC002",urgency="low"} 1' in measured.stdout
    assert "PSVC001" not in measured.stdout
    for p in tmp_path.rglob("*"):
        if p.is_file() and p.suffix in (".json", ".jsonl", ".log", ".prom"):
            text = p.read_text(errors="replace")
            assert CANARY not in text and REAL not in text, p
    assert CANARY not in log + measured.stdout + measured.stderr and REAL not in log


def test_clearance(tmp_path):
    d = scaffold("pagerduty", "pd_alerts", tmp_path)
    m = read(d / "poller.toml")
    require_clearance(m, "http://127.0.0.1:9999")                  # a fake: always
    with pytest.raises(MachineFileError, match="not cleared"):
        require_clearance(m, "https://api.pagerduty.com")
    (d / "poller.toml").write_text((d / "poller.toml").read_text().replace('"needs approval"', '"approved"'))
    require_clearance(read(d / "poller.toml"), "https://api.pagerduty.com")
    (d / "poller.toml").write_text((d / "poller.toml").read_text().replace("\n[settings]\n", '\n[settings]\ntoken = "x"\n'))
    with pytest.raises(MachineFileError, match="token"):
        read(d / "poller.toml")


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


def test_the_runner_reads_credentials_only_for_the_real_host(tmp_path, monkeypatch):
    """K4: against a fake on this machine the runner reads no token and no account (placeholders
    instead); against the real host, the references poller.toml names."""
    d = scaffold("jira", "jira_poll", tmp_path / "root")
    secret = tmp_path / "secrets" / "jira-token"
    secret.parent.mkdir()
    secret.write_text(REAL + "\n")
    secret.chmod(0o600)
    (d / "poller.toml").write_text((d / "poller.toml").read_text().replace(
        "\n[settings]\n", f'\n[settings]\ntoken_file = "{secret}"\nuser_env = "JIRA_EMAIL"\n'))
    monkeypatch.setenv("JIRA_EMAIL", "owner@example.com")
    m = read(d / "poller.toml")
    assert credentials(m, "http://127.0.0.1:9999") == {"token": FAKE_TOKEN, "basic_user": FAKE_USER}
    real = credentials(m, "https://jira.example.com")
    assert real == {"token_env": "JIRA_API_TOKEN", "token_file": str(secret), "basic_user": "owner@example.com",
                    "repo_root": None}
    (d / "poller.toml").write_text((d / "poller.toml").read_text().replace(
        "\n[settings]\n", '\n[settings]\nfake_token = "the-fakes-own"\n'))
    assert credentials(read(d / "poller.toml"), "http://localhost:1")["token"] == "the-fakes-own"


def test_a_real_token_is_never_sent_to_a_fake(tmp_path, monkeypatch):
    """The variable and the file both hold a real token, and the fake still gets the placeholder."""
    d = scaffold("pagerduty", "pd_alerts", tmp_path / "root")
    secret = tmp_path / "secrets" / "pd-token"
    secret.parent.mkdir()
    secret.write_text(REAL + "\n")
    secret.chmod(0o600)
    (d / "poller.toml").write_text((d / "poller.toml").read_text().replace(
        "\n[settings]\n", f'\n[settings]\ntoken_file = "{secret}"\n'))
    with FakePagerDuty(token=FAKE_TOKEN) as fake:
        r = subprocess.run([sys.executable, str(d / "pd_alerts.py"), "measure", "--base-url", fake.url, "--state-dir",
                            str(tmp_path / "state"), "--ttl", "0"], capture_output=True, text=True, timeout=60,
                           env=checkout_env(ORCHESTRATOR_HOME=str(tmp_path / "home"), PAGERDUTY_TOKEN=REAL), check=False)
        assert r.returncode == 0, r.stderr
        assert fake.requests and all(q.authorized for q in fake.requests)
    with FakePagerDuty(token=REAL) as fake:          # a fake that wants the real token never gets it
        r = subprocess.run([sys.executable, str(d / "pd_alerts.py"), "measure", "--base-url", fake.url, "--state-dir",
                            str(tmp_path / "state"), "--ttl", "0"], capture_output=True, text=True, timeout=60,
                           env=checkout_env(ORCHESTRATOR_HOME=str(tmp_path / "home"), PAGERDUTY_TOKEN=REAL), check=False)
        assert r.returncode != 0 and not any(q.authorized for q in fake.requests)
    assert REAL not in r.stdout + r.stderr


def test_a_fake_needs_no_token(tmp_path, monkeypatch):
    monkeypatch.delenv("PAGERDUTY_TOKEN", raising=False)
    d = scaffold("pagerduty", "pd_alerts", tmp_path / "root")
    with FakePagerDuty() as fake:                     # a fake that checks no token
        r = _measure(d, fake, tmp_path)
    assert r.returncode == 0 and 'pagerduty_incidents_open{service="PSVC001",urgency="high"} 1' in r.stdout, r.stderr


OWNERS = {     # what the owner's answers look like once filled in: another variable, their own scope
    "github": [('repo = "acme/service"', 'repo = "theirs/their-service"'), ("branches = []", 'branches = ["agent/*"]')],
    "jira": [('jql = "project = ABC"', 'jql = "project = XYZ AND assignee = currentUser()"')],
    "slack": [('channels = ["C001"]', 'channels = ["C777", "C888"]')],
    "discord": [('channels = ["C001"]', 'channels = ["777000000000000001"]'), ('guild = "1000"', 'guild = "4242"')],
    "pagerduty": [('open_statuses = ["triggered", "acknowledged"]', 'open_statuses = ["triggered"]')],
    "promql": [("query = \"queue_depth\"", "query = 'sum(rate(orders_total[5m]))'")],
    "http": [('system = "example"', 'system = "jenkins"'), ('list_path = "/api/items"', 'list_path = "/api/json"')],
}


@pytest.mark.parametrize("kind", sorted(KINDS))
def test_each_kinds_own_test_still_passes_with_the_owners_values(kind, tmp_path):
    """The scaffolded test reads the token's variable and the scope from poller.toml: after the
    owner's values and approval are written (as approve_integration does), it still passes."""
    d = scaffold(kind, f"my_{kind}", tmp_path / "root")
    toml = (d / "poller.toml").read_text()
    for old, new in [*OWNERS[kind], (f'token_env = "{KINDS[kind].token_env}"', 'token_env = "OWNERS_OWN_TOKEN"'),
                     ('clearance = "needs approval"', 'clearance = "approved"')]:
        assert old in toml, old
        toml = toml.replace(old, new, 1)
    (d / "poller.toml").write_text(toml)
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(d / f"test_my_{kind}.py")],
                       capture_output=True, text=True, timeout=300, cwd=tmp_path, env=checkout_env(), check=False)
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-2000:]


def test_the_jira_kits_own_test_passes_for_cloud_too(tmp_path):
    """Jira Cloud: the owner's site on atlassian.net makes the flavour cloud (v3, /search/jql, Basic)."""
    d = scaffold("jira", "jira_cloud", tmp_path / "root")
    toml = (d / "poller.toml").read_text().replace('"https://jira.example.com"', '"https://acme.atlassian.net"')
    (d / "poller.toml").write_text(toml.replace("\n[settings]\n", '\n[settings]\nuser_env = "JIRA_EMAIL"\n'))
    r = subprocess.run([sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", str(d / "test_jira_cloud.py")],
                       capture_output=True, text=True, timeout=300, cwd=tmp_path, env=checkout_env(), check=False)
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-2000:]
    assert "13 passed" in r.stdout


def test_guessed_values_are_marked():
    """A tool reading a template knows which values to ask the owner for."""
    for kind, keys in {"github": ["repo"], "jira": ["base_url", "edition", "jql"], "slack": ["channels"], "discord": ["guild", "channels"], "promql": ["base_url", "aliases"],
                       "http": ["base_url", "system", "thing", "list_path"]}.items():
        lines = (TEMPLATES / kind / "poller.toml").read_text().splitlines()
        for key in keys:
            [line] = [x for x in lines if x.startswith(f"{key} =")]
            assert "# guess:" in line, (kind, line)


def test_the_runner_set_overrides_only_the_kits_own_settings(tmp_path, monkeypatch):
    from orchestra_bridges.kits.runner import overrides
    assert overrides(['jql="project = A"', "channels=['C1']", "n=3"]) == {"jql": "project = A", "channels": ["C1"],
                                                                            "n": 3}
    for bad in ["token_env=\"X\"", "base_url=\"http://x\"", "jql=project = A", "nokey"]:
        with pytest.raises(ValueError):
            overrides([bad])


def test_an_uncleared_poller_waits_for_approval_instead_of_exiting(tmp_path):
    """A [[machines]] entry written before the owner's approval does not crash-loop: the poller logs
    once, reads nothing, re-reads poller.toml on its interval, and polls once it is cleared."""
    d = scaffold("pagerduty", "pd_alerts", tmp_path / "root")
    ev = tmp_path / "events.jsonl"
    env = checkout_env(PAGERDUTY_TOKEN=REAL, ORCHESTRATOR_HOME=str(tmp_path / "home"))
    with FakePagerDuty(token=TOKEN) as fake:
        proc = subprocess.Popen([sys.executable, str(d / "pd_alerts.py"), "--every", "0.2", "--events-file", str(ev),
                                 "--state-dir", str(tmp_path / "state"), "--ttl", "0"], env=env,
                                stderr=subprocess.PIPE, text=True)
        try:
            time.sleep(2.0)
            assert proc.poll() is None and not ev.exists() and not fake.requests     # alive, reading nothing
            toml = (d / "poller.toml").read_text()                                   # the fake is configured:
            (d / "poller.toml").write_text(toml.replace('"https://api.pagerduty.com"', f'"{fake.url}"'))
            deadline = time.time() + 30
            while time.time() < deadline and len(events_of(ev)) < 2:
                time.sleep(0.05)
            proc.send_signal(signal.SIGTERM)
            assert proc.wait(timeout=15) == 0
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
        log = proc.stderr.read()
    assert log.count("waiting for approval") == 1 and "approved: polling" in log, log
    assert [e["subject"] for e in events_of(ev) if e["type"].startswith("pagerduty.incident.")][:2] == [
        "PINC001", "PINC002"]


def test_an_uncleared_poller_stops_cleanly_while_waiting(tmp_path):
    d = scaffold("pagerduty", "pd_alerts", tmp_path / "root")
    log = tmp_path / "log"
    with log.open("w") as err:
        proc = subprocess.Popen([sys.executable, str(d / "pd_alerts.py"), "--every", "60", "--events-file",
                                 str(tmp_path / "e.jsonl"), "--state-dir", str(tmp_path / "state")],
                                env=checkout_env(ORCHESTRATOR_HOME=str(tmp_path / "home")), stderr=err)
        try:
            deadline = time.time() + 30
            while time.time() < deadline and "waiting for approval" not in log.read_text():
                time.sleep(0.05)
            assert proc.poll() is None, log.read_text()
            proc.send_signal(signal.SIGTERM)
            assert proc.wait(timeout=15) == 0
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.wait()
    assert log.read_text().count("waiting for approval") == 1 and not (tmp_path / "e.jsonl").exists()


def test_a_measurement_only_kit_says_nothing_of_a_poller_and_refuses_to_poll(tmp_path):
    d = scaffold("promql", "prom_series", tmp_path / "root")
    doc = (d / "AGENTS.md").read_text()
    assert "a measurement of" in doc and "Poller.emit" not in doc and "--once" not in doc and "{{" not in doc
    assert "[[machines]]" not in doc and "make_poll" not in (d / "prom_series.py").read_text()
    r = subprocess.run([sys.executable, str(d / "prom_series.py"), "--state-dir", str(tmp_path / "st")],
                       capture_output=True, text=True, timeout=60, check=False)
    assert r.returncode == 2 and "polls nothing" in r.stderr, r.stderr
    poller = (scaffold("pagerduty", "pd_doc", tmp_path / "root") / "AGENTS.md").read_text()
    assert "Poller.emit" in poller and "person.pagerduty.<account>" in poller and "{{" not in poller
    http = (scaffold("http", "api_doc", tmp_path / "root") / "AGENTS.md").read_text()
    assert "person.<system>.<account>" in http and "person.http." not in http
