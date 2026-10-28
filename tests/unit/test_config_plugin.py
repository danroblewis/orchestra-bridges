import sys
from pathlib import Path

import pytest
from orchestra.config import Config, ConfigError, Project

from orchestra_bridges import config as cfg
from orchestra_bridges.plugin import plugin


def proj(toml: str) -> Project:
    return Project("k", Path("/repo"), Config.from_toml(toml))


def test_defaults_and_durations():
    c = cfg.github(proj('[bridges.github]\nrepos = ["acme/service"]\n').config.raw, Path("/repo"))
    assert (c.repos, c.every, c.lookback, c.max_catchup, c.bots_answer, c.people, c.port) == (
        ["acme/service"], 60.0, 3600.0, 172800.0, False, None, 9464)
    c = cfg.github(proj('[bridges]\npeople = "p.toml"\n[bridges.github]\nrepos = "a/b"\nevery = "2m"\n').config.raw,
                   Path("/repo"))
    assert (c.repos, c.every, c.people) == (["a/b"], 120.0, Path("/repo/p.toml"))


@pytest.mark.parametrize("toml", ['[bridges.github]\nrepos = ["vscode"]\n', '[bridges.github]\nevery = "1s"\n', "",
                                  '[bridges.github]\nport = "x"\n', "[bridges.github]\n", '[bridges.github]\nrepos = []\n'])
def test_bad_settings_are_refused(toml):
    with pytest.raises(ConfigError):
        cfg.github(proj(toml).config.raw, Path("/repo"))


def test_the_plugin_runs_the_bridge_only_where_it_is_configured_and_declares_no_machine():
    off = plugin(proj("[events]\nurl = 'file'\n"))
    assert (off.name, off.processes, off.machines, off.pages, off.collectors) == ("bridges", [], [], [], [])
    assert off.guide.is_file() and off.guide.name == "guide.md"                       # its operator's guide, configured or not
    on = plugin(proj("[bridges.github]\n"))
    [p] = on.processes
    assert (p.name, p.argv()) == ("github", [sys.executable, "-m", "orchestra_bridges", "github"])
    assert p.config_keys == ["bridges.github", "bridges.people"]      # a changed setting restarts it
    assert on.machines == []                            # a bridge is a relay: its actors are the machines
    [c] = on.collectors
    assert (c.name, c.kind, c.target) == ("github", "scrape", "http://127.0.0.1:9464/metrics")
    assert plugin(proj("[bridges.github]\nport = 0\n")).collectors == []


def test_orchestra_finds_the_installed_plugin():
    from orchestra.plugins import load

    loaded = load(proj("[bridges.github]\n"))
    assert "bridges" in [p.name for p in loaded.plugins] and not loaded.errors


def test_the_pull_request_bridge_settings():
    c = cfg.github_prs(proj('[bridges.github_prs]\nrepos = ["a/b"]\nbranches = ["agent/*"]\nlabels = "SAIL"\n'
                            '[bridges.github_prs.label_results]\nSAIL = ["SAIL-passed", "SAIL-failed"]\n').config.raw,
                       Path("/repo"))
    assert (c.repos, c.branches, c.labels, c.label_results, c.port, c.checks) == (
        ["a/b"], ["agent/*"], ["SAIL"], {"SAIL": ["SAIL-passed", "SAIL-failed"]}, 9466, True)
    assert c.follows("agent/x", []) and c.follows("main", ["SAIL"]) and not c.follows("feature/x", ["bug"])
    every = cfg.github_prs(proj('[bridges.github_prs]\nrepos = ["a/b"]\n').config.raw, Path("/repo"))
    assert every.follows("anything", [])                # with neither branches nor labels, every pull request


@pytest.mark.parametrize("toml", ['[bridges.github_prs]\n', '[bridges.github_prs]\nrepos = ["vscode"]\n',
                                  '[bridges.github_prs]\nrepos = ["a/b"]\ntoken = "ghp_x"\n',
                                  '[bridges.github_prs]\nrepos = ["a/b"]\nport = "x"\n',
                                  '[bridges.github_prs]\nrepos = ["a/b"]\n[bridges.github_prs.label_results]\nSAIL = "ok"\n'])
def test_bad_pull_request_bridge_settings_are_refused(toml):
    with pytest.raises(ConfigError):
        cfg.github_prs(proj(toml).config.raw, Path("/repo"))


def test_the_plugin_runs_the_pull_request_bridge_and_declares_its_instrument():
    on = plugin(proj('[bridges.github_prs]\nrepos = ["a/b"]\nhost = "0.0.0.0"\n'))
    assert [(p.name, p.argv()[-1]) for p in on.processes] == [("github_prs", "github_prs")]
    assert on.machines == []
    [c] = on.collectors
    assert (c.name, c.kind, c.target) == ("github_prs", "scrape", "http://127.0.0.1:9466/metrics")
    assert plugin(proj('[bridges.github_prs]\nrepos = ["a/b"]\nport = 0\n')).collectors == []


def test_the_jira_bridge_settings():
    c = cfg.jira(proj('[bridges.jira]\napi = "https://jira.acme.com/"\nprojects = "ABC"\n').config.raw, Path("/repo"))
    assert (c.name, c.api, c.deployment, c.projects, c.port, c.site, c.env) == (
        "jira", "https://jira.acme.com", "datacenter", ["ABC"], 9467, "jira.acme.com", "JIRA")
    assert c.scope() == 'project in ("ABC")'
    cloud = cfg.jira(proj('[bridges.jira]\napi = "https://acme.atlassian.net"\njql = "project = ABC"\n').config.raw,
                     Path("/repo"))
    assert (cloud.deployment, cloud.scope()) == ("cloud", "project = ABC")
    fed = cfg.jira(proj('[bridges.jira_fedramp]\napi = "https://jira.fed.acme.com"\nprojects = ["CUST"]\nport = 9468\n'
                        ).config.raw, Path("/repo"), "jira_fedramp")
    assert (fed.env, fed.port) == ("JIRA_FEDRAMP", 9468)


@pytest.mark.parametrize("name,toml", [
    ("jira", '[bridges.jira]\nprojects = ["ABC"]\n'),                                         # no api
    ("jira", '[bridges.jira]\napi = "http://jira.acme.com"\nprojects = ["ABC"]\n'),            # plain http elsewhere
    ("jira", '[bridges.jira]\napi = "https://j.acme.com"\n'),                                  # neither projects nor jql
    ("jira", '[bridges.jira]\napi = "https://j.acme.com"\nprojects = ["ABC"]\njql = "x"\n'),   # both
    ("jira", '[bridges.jira]\napi = "https://j.acme.com"\nprojects = ["abc"]\n'),
    ("jira", '[bridges.jira]\napi = "https://j.acme.com"\njql = "project = A ORDER BY key"\n'),
    ("jira", '[bridges.jira]\napi = "https://j.acme.com"\nprojects = ["ABC"]\ntoken = "secret"\n'),
    ("jira", '[bridges.jira]\napi = "https://j.acme.com"\nprojects = ["ABC"]\ndeployment = "server"\n'),
    ("jira_fed", '[bridges.jira_fed]\napi = "https://j.acme.com"\nprojects = ["ABC"]\n'),       # a second site needs a port
])
def test_bad_jira_settings_are_refused(name, toml):
    with pytest.raises(ConfigError):
        cfg.jira(proj(toml).config.raw, Path("/repo"), name)


def test_the_plugin_runs_each_jira_site_and_declares_its_instrument():
    on = plugin(proj('[bridges.jira]\napi = "https://j.acme.com"\nprojects = ["ABC"]\n'
                     '[bridges.jira_fedramp]\napi = "https://f.acme.com"\nprojects = ["CUST"]\nport = 9468\n'))
    assert [(p.name, p.argv()[-1]) for p in on.processes] == [("jira", "jira"), ("jira_fedramp", "jira_fedramp")]
    assert on.machines == []
    assert [(c.name, c.target) for c in on.collectors] == [("jira", "http://127.0.0.1:9467/metrics"),
                                                           ("jira_fedramp", "http://127.0.0.1:9468/metrics")]
    assert plugin(proj('[bridges.jira]\napi = "https://j.acme.com"\nprojects = ["ABC"]\nport = 0\n')).collectors == []


def test_an_empty_github_section_follows_nothing():
    """No default repository: a section without repos is refused, with what to write."""
    with pytest.raises(ConfigError, match=r"repos is required.*owner/name"):
        cfg.github(proj("[bridges.github]\n").config.raw, Path("/repo"))


def test_config_edit_runs_the_bridges_check(tmp_path, monkeypatch):
    """Plugin.check: Orchestra's config_edit refuses what the bridges' check newly says, by name."""
    import subprocess

    from orchestra import config_edit
    from orchestra.config import OVERLAY_ENV, init_project

    monkeypatch.setenv("ORCHESTRATOR_HOME", str(tmp_path / "home"))
    monkeypatch.delenv(OVERLAY_ENV, raising=False)
    monkeypatch.setattr(config_edit, "_LOADED", {})
    r = tmp_path / "repo"
    r.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=r, check=True)
    (r / "orchestra.toml").write_text('[metrics]\nurl = "file"\n')
    project = init_project(r)
    with pytest.raises(ConfigError, match=r"bridges: \[bridges.github\] repos is required"):
        project.config_edit.set("bridges.github.every", "2m")
    c = project.config_edit.set_many({"bridges.github.repos": ["acme/service"], "bridges.github.every": "2m"})
    assert c.changed and not c.warnings
    with pytest.raises(ConfigError, match=r"bridges: \[bridges.github\] has no setting interval"):
        project.config_edit.set("bridges.github.interval", "1m")
    project.config_edit.unset("bridges")                                   # removing the section: allowed
    assert "bridges" not in project.config.raw
