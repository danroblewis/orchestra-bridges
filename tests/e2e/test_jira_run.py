"""``orchestra run`` starts the Jira bridge the plugin declares, against a fake Data Center with the
token in the environment; the bridge publishes the fake's issues as the people who acted, and the
instrument ``bridges.jira`` (here the project's own ``[[scrape]]`` of that name, every second, in
place of the plugin's every 30 s) puts the levels in the store. Stopping ``orchestra run`` stops it."""

import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

from fake_jira import CANARY, TOKEN, FakeJira
from orchestra.config import load_project
from orchestra.events import Events
from orchestra.metrics import for_project


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_orchestra_run_keeps_the_jira_bridge_running_and_scrapes_it(tmp_path, monkeypatch):
    port = free_port()
    monkeypatch.setenv("ORCHESTRATOR_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("ORCHESTRA_EVENTS_URL", raising=False)
    monkeypatch.delenv("ORCHESTRA_METRICS_URL", raising=False)
    with FakeJira() as jira:
        now = time.time()
        key = jira.create("ABC", "alice", now - 600, assignee="owner")
        jira.comment(key, "bob", now - 300)
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
        (repo / "orchestra.toml").write_text(f"""[bridges.jira]
api = "{jira.base}"
projects = ["ABC"]
port = {port}

[[scrape]]
name = "bridges.jira"             # replaces the plugin's scrape of the same port
url = "http://127.0.0.1:{port}/metrics"
every = "1s"
""")
        project = load_project(repo)
        orchestra = Path(sys.executable).with_name("orchestra")
        env = {**os.environ, "JIRA_API_TOKEN": TOKEN}
        host = subprocess.Popen([str(orchestra), "run"], cwd=repo, env=env, stdout=subprocess.DEVNULL,
                                stderr=subprocess.STDOUT, start_new_session=True)
        store = for_project(project)
        labels = {"instrument": "bridges.jira", "jira_project": "ABC"}
        try:
            end = time.time() + 40
            while time.time() < end and not store.latest("jira_comments_unanswered", labels):
                time.sleep(0.5)
            evs = [e for e in Events.for_project(project).after(0)[0] if e["type"].startswith("jira.")]
            assert [(e["type"], e["source"], e["subject"]) for e in evs] == [
                ("jira.issue.created", "person.jira.alice", key), ("jira.issue.assigned", "person.jira.alice", key),
                ("jira.comment.added", "person.jira.bob", key)]
            assert store.latest("jira_comments_unanswered", labels)[1] == 1
            assert store.latest("jira_issues_mine_not_started", labels)[1] == 1
        finally:
            host.send_signal(signal.SIGTERM)
            host.wait(timeout=30)
    log = (project.logs_dir / "machines" / "bridges.jira.log").read_text()
    assert "bridges.jira: stopped" in log and TOKEN not in log and CANARY not in log
    everything = json.dumps(Events.for_project(project).after(0)[0])
    assert CANARY not in everything and TOKEN not in everything
