"""``orchestra run`` starts the Slack bridge the plugin declares; the bridge publishes the fake's pings
and their answers, and the project's ``[[scrape]]`` of its ``/metrics`` puts the stock and the
counters in the store, labelled as the bridge's own machine. Stopping ``orchestra run`` stops it.

The bridge it starts imports ``orchestra_bridges`` from this checkout (``conftest.checkout_env``), not
from wherever the environment's editable install points, so the test runs the code under test."""

import json
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

from conftest import checkout_env, new_project
from fake_slack import CANARY, CHANNEL, OWNER, TEAM, TOKEN, FakeSlack
from orchestra.events import Events
from orchestra.metrics import for_project


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_orchestra_run_keeps_the_slack_bridge_running_and_scrapes_it(tmp_path, monkeypatch):
    port = free_port()
    with FakeSlack() as slack:
        now = time.time()
        slack.user("U0BOB", "bob")
        answered = slack.say("U0BOB", CHANNEL, now - 30, mentions=(OWNER,))      # seen answered at the first poll:
        slack.say("U0BOB", slack.dm("U0BOB"), now - 20)                             # within 5 minutes
        slack.react(CHANNEL, answered, "white_check_mark", OWNER, now - 10)
        project = new_project(tmp_path, monkeypatch, f"""[bridges.slack]
port = {port}
api = "{slack.base}"

[[scrape]]
name = "bridges.slack"
url = "http://127.0.0.1:{port}/metrics"
every = "1s"
""")
        orchestra = Path(sys.executable).with_name("orchestra")
        env = checkout_env(SLACK_USER_TOKEN=TOKEN)
        host = subprocess.Popen([str(orchestra), "run"], cwd=project.repo_root, env=env, stdout=subprocess.DEVNULL,
                                stderr=subprocess.STDOUT, start_new_session=True)
        store = for_project(project)
        labels = {"machine": "bridges.slack", "workspace": TEAM}
        try:
            end = time.time() + 40
            while time.time() < end:
                got = store.latest("slack_pings_answered_total", {**labels, "within": "5m"})
                if got and got[1] == 1:
                    break
                time.sleep(0.5)
            evs = [e for e in Events.for_project(project).after(0)[0] if e["type"].startswith("slack.ping.")]
            assert [(e["type"], e["source"]) for e in evs] == [
                ("slack.ping.received", "person.slack.bob"), ("slack.ping.received", "person.slack.bob"),
                ("slack.ping.answered", "person.slack.daniel")]
            assert store.latest("slack_pings_unanswered", labels)[1] == 1
            assert store.latest("slack_pings_answered_total", {**labels, "within": "5m"})[1] == 1
        finally:
            host.send_signal(signal.SIGTERM)
            host.wait(timeout=30)
    log = (project.logs_dir / "machines" / "bridges.slack.log").read_text()
    assert "bridges.slack: stopped" in log and TOKEN not in log and CANARY not in log
    everything = json.dumps(Events.for_project(project).after(0)[0])
    assert CANARY not in everything and TOKEN not in everything
