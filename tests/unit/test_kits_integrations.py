"""``orchestra bridges integrations``: read-only, the kits and the machines scaffolded from them,
with clearance, the token's variable NAME (never a value), whether they are scheduled, and the
guesses left; another machine spec in the state directory is not listed."""

import json

from conftest import new_project

from orchestra_bridges.__main__ import main as bridges_main
from orchestra_bridges.kits.integrations import listing
from orchestra_bridges.kits.scaffold import entries, root_dir, scaffold


def test_lists_scaffolded_machines_with_clearance_token_name_schedule_and_guesses(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("JIRA_API_TOKEN", "a-real-looking-secret-value")
    proj = new_project(tmp_path, monkeypatch, "")
    pd = scaffold("pagerduty", "pd_alerts", proj.state_dir)
    scaffold("jira", "jira_poll", root_dir(proj, "repo"))
    scaffold("promql", "chrono", proj.state_dir)
    other = proj.state_dir / "machines" / "pipeline_manager"            # an orchestra-agents spec: not ours
    other.mkdir(parents=True)
    (other / "machine.toml").write_text('name = "pipeline_manager"\nkind = "agent"\n')
    toml = pd / "poller.toml"
    toml.write_text(toml.read_text().replace('clearance = "needs approval"', 'clearance = "approved"')
                    .replace('token_env = "PAGERDUTY_TOKEN"', 'token_env = "PD_READONLY"'))
    (proj.repo_root / "orchestra.toml").write_text(entries("pd_alerts", pd) + "\n")
    proj.reload_config()

    items = {i.name: i for i in listing(proj)}
    assert sorted(items) == ["chrono", "jira_poll", "pd_alerts"]
    p, j, c = items["pd_alerts"], items["jira_poll"], items["chrono"]
    assert (p.kit, p.root, p.clearance, p.token, p.schedule, p.collect_entry, p.guesses) == (
        "pagerduty", "state", "approved", "$PD_READONLY", "[[machines]]", True, 0)
    assert (j.root, j.clearance, j.token, j.schedule, j.collect_entry) == (
        "repo", "needs approval", "$JIRA_API_TOKEN", "no", False)
    assert j.guesses == 2                                                # the site and the JQL
    assert c.schedule is None and c.guesses >= 2                  # a measurement only: no [[machines]] wanted

    assert bridges_main(["integrations"]) == 0
    out = capsys.readouterr().out
    assert "kits: discord, github, http, jira, pagerduty, promql, slack" in out and "$PD_READONLY" in out
    assert "a-real-looking-secret-value" not in out and "pipeline_manager" not in out
    assert bridges_main(["integrations", "--json"]) == 0
    got = json.loads(capsys.readouterr().out)
    assert {m["name"] for m in got["machines"]} == {"chrono", "jira_poll", "pd_alerts"}


def test_an_empty_project_says_how_to_start(tmp_path, monkeypatch, capsys):
    new_project(tmp_path, monkeypatch, "")
    assert bridges_main(["integrations"]) == 0
    out = capsys.readouterr().out
    assert "orchestra bridges scaffold" in out and "no machines scaffolded" in out


def test_a_broken_machine_file_is_listed_with_its_error(tmp_path, monkeypatch):
    proj = new_project(tmp_path, monkeypatch, "")
    d = scaffold("slack", "chat", proj.state_dir)
    (d / "poller.toml").write_text((d / "poller.toml").read_text().replace("\n[settings]\n",
                                                                           '\n[settings]\ntoken = "x"\n'))
    [i] = listing(proj)
    assert i.name == "chat" and "token" in i.error and i.token == "?"


def test_a_poller_the_workflows_floor_runs_is_scheduled_by_every(tmp_path, monkeypatch):
    proj = new_project(tmp_path, monkeypatch, "")
    d = scaffold("jira", "jira_poll", proj.state_dir)
    (proj.repo_root / "orchestra.toml").write_text('[workflows.every.jira_poll]\nevery = "5m"\nrole = "jira_poll"\nbrief = "poll"\n')
    proj.reload_config()
    assert listing(proj)[0].schedule == "no"                     # the clock item, but no machine.json the floor runs it from
    (d / "machine.json").write_text('{"id": "jira_poll"}\n')
    assert listing(proj)[0].schedule == "every"


def test_a_poller_scaffolded_as_machine_toml_is_still_read(tmp_path, monkeypatch):
    """Before 2026-11-01 the scaffold wrote ``machine.toml``: a folder holding one with a ``kit`` key
    (and no ``poller.toml``) is still a poller's, listed and run; one without is an agent's spec."""
    from orchestra_bridges.kits.machinefile import locate

    proj = new_project(tmp_path, monkeypatch, "")
    d = scaffold("jira", "old_jira", proj.state_dir)
    (d / "poller.toml").rename(d / "machine.toml")
    assert locate(d) == d / "machine.toml"
    assert [i.name for i in listing(proj)] == ["old_jira"]
    (d / "machine.toml").write_text('name = "old_jira"\nkind = "agent"\n')
    assert locate(d) == d / "poller.toml" and listing(proj) == []
