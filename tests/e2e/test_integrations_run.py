"""The integration examples run from an Orchestra project as Orchestra runs them: ``orchestra run``
keeps the three pollers going as ``[[machines]]`` processes against their fakes (no credentials)
and stores their measurements through ``[[collect]]`` entries; after a run, the Machines page's
``/api/machines`` (``orchestra web`` on a free port above 8100) shows the people and bots they
relayed as machines, observed (not unseen), and never the pollers themselves."""

import importlib.util
import json
import os
import random
import signal
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

from conftest import ROOT, checkout_env, new_project
from orchestra.metrics import for_project

from orchestra_bridges.kits.fakes import CANARY, FakeJira, FakePagerDuty, FakeSlack

EXAMPLES = ROOT / "examples" / "integrations"


def world(name: str) -> dict:
    """The example's own test world, its times moved to now."""
    spec = importlib.util.spec_from_file_location(f"e2e_{name}", EXAMPLES / name / f"test_{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.recent_world()


POLLERS = ("jira_poller", "slack_watcher", "pagerduty_alerts")
ACTORS = {"person.jira.alice", "person.jira.bob", "person.slack.u001", "person.slack.u002", "bot.slack.b001",
          "bot.pagerduty", "person.pagerduty.puser01"}


def free_port() -> int:
    """A free port above 8100 (the never-list's ports are all below it but 8428 and 9090)."""
    for port in random.sample(range(8101, 8999), 200):
        if port == 8428:
            continue
        with socket.socket() as s:
            try:
                s.bind(("127.0.0.1", port))
            except OSError:
                continue
            return port
    raise RuntimeError("no free port above 8100")


def toml(urls: dict[str, str]) -> str:
    py = json.dumps(sys.executable)
    out = ['[web]\nauth = "off"\n']
    for name in POLLERS:
        prog = json.dumps(str(EXAMPLES / name / f"{name}.py"))[1:-1]
        args = f"--base-url {urls[name]} --every 2 --ttl 0"
        out.append(f'[[machines]]\nname = "{name}"\ncommand = "{py[1:-1]} {prog} {args}"\nrestart = "always"\n')
        out.append(f'[[collect]]\nname = "{name}"\ncommand = "{py[1:-1]} {prog} measure --base-url {urls[name]} --ttl 0"\n'
                   'every = "2s"\ntimeout = "2s"\n')
    return "\n".join(out)


def get(url: str) -> dict | None:
    try:
        with urllib.request.urlopen(url, timeout=5) as r:
            return json.loads(r.read())
    except (OSError, ValueError):
        return None


def stop(proc: subprocess.Popen) -> None:
    if proc.poll() is None:
        os.killpg(proc.pid, signal.SIGTERM)
        try:
            proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()


def test_orchestra_run_shows_the_actors_as_machines(tmp_path, monkeypatch):
    for v in ("JIRA_API_TOKEN", "SLACK_TOKEN", "PAGERDUTY_TOKEN"):
        monkeypatch.delenv(v, raising=False)                     # the fakes need none
    with FakeJira(world("jira_poller")) as jira, FakeSlack(world("slack_watcher")) as slack, FakePagerDuty() as pd:
        urls = {"jira_poller": jira.url, "slack_watcher": slack.url, "pagerduty_alerts": pd.url}
        project = new_project(tmp_path, monkeypatch, toml(urls))
        orchestra, port = Path(sys.executable).with_name("orchestra"), free_port()
        env = checkout_env(ORCHESTRATOR_HOME=os.environ["ORCHESTRATOR_HOME"], ORCHESTRA_REPO=str(project.repo_root))
        logs = open(tmp_path / "orchestra.log", "w")  # noqa: SIM115 - closed in the finally, after both processes
        host = subprocess.Popen([str(orchestra), "run"], cwd=project.repo_root, env=env, stdout=logs,
                                stderr=subprocess.STDOUT, start_new_session=True)
        web = subprocess.Popen([str(orchestra), "web", "--port", str(port)], cwd=project.repo_root, env=env,
                               stdout=logs, stderr=subprocess.STDOUT, start_new_session=True)
        try:
            deadline, seen, d = time.time() + 150, {}, None
            while time.time() < deadline:
                d = get(f"http://127.0.0.1:{port}/api/machines")
                if d and d.get("ok"):
                    seen = {m["id"]: m for m in d["machines"]}
                    if ACTORS <= set(seen):
                        break
                time.sleep(1)
            pd.set_status("PINC001", "resolved", by="PUSER02")       # a change between intervals
            while time.time() < deadline and "person.pagerduty.puser02" not in seen:
                d = get(f"http://127.0.0.1:{port}/api/machines") or {}
                seen = {m["id"]: m for m in d.get("machines", [])}
                time.sleep(1)
            store = for_project(project)
            stored = {name for name, _ in store.series("")}
        finally:
            stop(web)
            stop(host)
            logs.close()
    assert ACTORS | {"person.pagerduty.puser02"} <= set(seen), (sorted(seen), (tmp_path / "orchestra.log").read_text()[-3000:])
    for a in ACTORS:
        m = seen[a]
        assert not m.get("unseen") and m["kind"] == ("program" if a.startswith("bot.") else "person"), m
        assert m["publishes"] and m["mode"] == "ad hoc"
    assert seen["person.jira.bob"]["package"] == "jira_poller"
    assert not set(POLLERS) & set(seen)                             # a relay is never a machine
    assert {"jira_issues_total", "jira_issue_age_seconds", "slack_mentions_open_total", "slack_messages_total",
            "pagerduty_incidents_open_total", "pagerduty_incidents_triggered_total"} <= stored, sorted(stored)
    state_logs = [p for p in project.state_dir.rglob("*") if p.is_file() and p.suffix in (".log", ".json", ".jsonl")]
    for p in state_logs:
        assert CANARY not in p.read_text(errors="replace"), p
