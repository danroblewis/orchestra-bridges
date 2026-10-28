"""A bridge publishes only as the people and bots who acted (``relay.py``), and the GitHub issues
bridge serves what it measures for its instrument instead of writing it."""

import threading
import urllib.request

import pytest
from conftest import T0, Clock, scrape
from orchestra.events import HOST, Events

from orchestra_bridges.identity import People
from orchestra_bridges.relay import Relay, RelayError


@pytest.mark.parametrize("source", ["bridges.github", "bridges.jira_fedramp", "bridges", HOST, "", None, "alice"])
def test_a_bridge_never_publishes_as_itself_or_as_no_one(tmp_path, source):
    relay = Relay(Events(tmp_path / "events.jsonl"))
    with pytest.raises(RelayError):
        relay.publish("github.issue.opened", {}, source=source)
    assert relay.after(0)[0] == []


def test_it_publishes_as_people_and_bots(tmp_path):
    relay = Relay(Events(tmp_path / "events.jsonl"))
    relay.publish("jira.issue.transitioned", {}, source="bot.jira")
    relay.publish("github.issue.opened", {}, source="person.alice")
    assert [e["source"] for e in relay.after(0)[0]] == ["bot.jira", "person.alice"]


def test_the_system_itself_is_a_bot_with_no_account():
    jira = People().itself("jira", "Jira")
    assert (jira.source, jira.login, jira.bot) == ("bot.jira", None, True)
    assert jira.announcement("jira") == {"kind": "program", "mode": "ad hoc", "package": "bridges", "bot": True,
                                         "name": "Jira"}


def test_the_github_issues_bridge_serves_each_change_at_its_own_time(project, github, make_bridge):
    github.open(1, "alice", T0 - 3000)
    github.comment(1, "bob", T0 - 2900)
    b = make_bridge()
    assert b.render() is None                           # 503 until it has read its past back
    b.clock = Clock(T0)
    stop = threading.Event()
    t = threading.Thread(target=b.run, args=(stop,), daemon=True)
    t.start()
    try:
        for _ in range(100):
            if b.server and b.measures.waiting:
                break
            stop.wait(0.05)
        with urllib.request.urlopen(b.server.url, timeout=5) as r:
            text = r.read().decode()
    finally:
        stop.set()
        t.join(timeout=10)
    opened = [line for line in text.splitlines() if line.startswith("github_issues_opened_total{")]
    assert opened and all(line.endswith(f" {round(T0 * 1000)}") or line.split()[-1].isdigit() for line in opened)
    assert 'repo="microsoft/vscode"' in opened[0] and "machine=" not in text
    assert b.render() == ""                             # taken by that scrape
    assert scrape(b, project, "bridges.github", T0) == 0
