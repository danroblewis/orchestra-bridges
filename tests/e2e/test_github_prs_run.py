"""``orchestra run`` starts the pull-request bridge the plugin declares; the bridge publishes what the
fake GitHub holds as the people who did it, and the instrument ``bridges.github_prs`` (here the
project's own ``[[scrape]]`` of that name, every second, in place of the plugin's every 30 s) puts
the levels in the store, labelled ``instrument="bridges.github_prs"``. Stopping ``orchestra run``
stops it."""

import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

from fake_github import CANARY, TOKEN
from fake_github_prs import FakePRs
from orchestra.config import load_project
from orchestra.events import Events
from orchestra.metrics import for_project

R = "acme/service"


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_orchestra_run_keeps_the_pull_request_bridge_running_and_scrapes_it(tmp_path, monkeypatch):
    port = free_port()
    monkeypatch.setenv("ORCHESTRATOR_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("ORCHESTRA_EVENTS_URL", raising=False)
    monkeypatch.delenv("ORCHESTRA_METRICS_URL", raising=False)
    with FakePRs(R) as gh:
        now = time.time()
        gh.open(1, "alice", now - 600, branch="agent/one")
        gh.request_review(1, "alice", "bob", now - 500)
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
        (repo / "orchestra.toml").write_text(f"""[bridges.github_prs]
repos = ["{R}"]
port = {port}
api = "{gh.base}"

[[scrape]]
name = "bridges.github_prs"       # replaces the plugin's scrape of the same port
url = "http://127.0.0.1:{port}/metrics"
every = "1s"
""")
        project = load_project(repo)
        orchestra = Path(sys.executable).with_name("orchestra")
        env = {**os.environ, "GH_ENTERPRISE_TOKEN": TOKEN}        # the fake is not github.com
        host = subprocess.Popen([str(orchestra), "run"], cwd=repo, env=env, stdout=subprocess.DEVNULL,
                                stderr=subprocess.STDOUT, start_new_session=True)
        store = for_project(project)
        labels = {"instrument": "bridges.github_prs", "repo": R}
        try:
            end = time.time() + 40
            while time.time() < end and not store.latest("github_prs_awaiting_review", labels):
                time.sleep(0.5)
            evs = [e for e in Events.for_project(project).after(0)[0] if e["type"].startswith("github.pr.")]
            assert [(e["type"], e["source"], e["subject"]) for e in evs] == [
                ("github.pr.opened", "person.alice", f"{R}#1"), ("github.pr.review_requested", "person.alice", f"{R}#1")]
            assert store.latest("github_prs_awaiting_review", labels)[1] == 1
            assert store.latest("github_prs_open", labels)[1] == 1
        finally:
            host.send_signal(signal.SIGTERM)
            host.wait(timeout=30)
    assert (project.logs_dir / "instruments" / "bridges.github_prs.log").exists()
    log = (project.logs_dir / "machines" / "bridges.github_prs.log").read_text()
    assert "bridges.github_prs: stopped" in log and TOKEN not in log and CANARY not in log
    everything = json.dumps(Events.for_project(project).after(0)[0])
    assert CANARY not in everything and TOKEN not in everything
