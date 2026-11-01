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
    toml = pd / "machine.toml"
    toml.write_text(toml.read_text().replace('clearance = "needs approval"', 'clearance = "approved"')
                    .replace('token_env = "PAGERDUTY_TOKEN"', 'token_env = "PD_READONLY"'))
    (proj.repo_root / "orchestra.toml").write_text(entries("pd_alerts", pd) + "\n")
    proj.reload_config()

    items = {i.name: i for i in listing(proj)}
    assert sorted(items) == ["chrono", "jira_poll", "pd_alerts"]
    p, j, c = items["pd_alerts"], items["jira_poll"], items["chrono"]
    assert (p.kit, p.root, p.clearance, p.token, p.machines_entry, p.collect_entry, p.guesses) == (
        "pagerduty", "state", "approved", "$PD_READONLY", True, True, 0)
    assert (j.root, j.clearance, j.token, j.machines_entry, j.collect_entry) == (
        "repo", "needs approval", "$JIRA_API_TOKEN", False, False)
    assert j.guesses == 2                                                # the site and the JQL
    assert c.machines_entry is None and c.guesses >= 2                  # a measurement only: no [[machines]] wanted

    assert bridges_main(["integrations"]) == 0
    out = capsys.readouterr().out
    assert "kits: http, jira, pagerduty, promql, slack" in out and "$PD_READONLY" in out
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
    (d / "machine.toml").write_text((d / "machine.toml").read_text().replace("\n[settings]\n",
                                                                           '\n[settings]\ntoken = "x"\n'))
    [i] = listing(proj)
    assert i.name == "chat" and "token" in i.error and i.token == "?"
