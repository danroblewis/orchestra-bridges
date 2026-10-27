"""``orchestra run`` starts the bridge the plugin declares, the bridge publishes what the fake GitHub
holds as the people who did it, and stopping ``orchestra run`` stops it."""

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

from conftest import published
from fake_github import TOKEN


def issues(project):
    return [e for e in published(project) if e["type"].startswith("issue.")]


def test_orchestra_run_keeps_the_bridge_running(project, github, tmp_path):
    now = time.time()
    github.open(1, "alice", now - 600)
    github.comment(1, "bob", now - 300)
    orchestra = Path(sys.executable).with_name("orchestra")
    env = {**os.environ, "GH_TOKEN": TOKEN}
    host = subprocess.Popen([str(orchestra), "run"], cwd=project.repo_root, env=env, stdout=subprocess.DEVNULL,
                            stderr=subprocess.STDOUT, start_new_session=True)
    try:
        end = time.time() + 30
        while time.time() < end and len(issues(project)) < 2:
            time.sleep(0.5)
        assert [(e["type"], e["source"]) for e in issues(project)] == [
            ("issue.opened", "person.alice"), ("issue.commented", "person.bob")]
    finally:
        host.send_signal(signal.SIGTERM)
        host.wait(timeout=30)
    log = (project.logs_dir / "machines" / "bridges.github.log").read_text()
    assert "bridges.github: stopped" in log and TOKEN not in log
