import sys
from pathlib import Path

import pytest
from orchestra.config import Config, ConfigError, Project

from orchestra_bridges import config as cfg
from orchestra_bridges.plugin import plugin


def proj(toml: str) -> Project:
    return Project("k", Path("/repo"), Config.from_toml(toml))


def test_defaults_and_durations():
    c = cfg.github(proj("[bridges.github]\n").config.raw, Path("/repo"))
    assert (c.repos, c.every, c.lookback, c.max_catchup, c.bots_answer, c.people) == (
        ["microsoft/vscode"], 60.0, 3600.0, 172800.0, False, None)
    c = cfg.github(proj('[bridges]\npeople = "p.toml"\n[bridges.github]\nrepos = "a/b"\nevery = "2m"\n').config.raw,
                   Path("/repo"))
    assert (c.repos, c.every, c.people) == (["a/b"], 120.0, Path("/repo/p.toml"))


@pytest.mark.parametrize("toml", ['[bridges.github]\nrepos = ["vscode"]\n', '[bridges.github]\nevery = "1s"\n', ""])
def test_bad_settings_are_refused(toml):
    with pytest.raises(ConfigError):
        cfg.github(proj(toml).config.raw, Path("/repo"))


def test_the_plugin_runs_the_bridge_only_where_it_is_configured():
    off = plugin(proj("[events]\nurl = 'file'\n"))
    assert (off.name, off.processes, off.machines, off.pages) == ("bridges", [], [], [])
    on = plugin(proj("[bridges.github]\n"))
    [p] = on.processes
    assert (p.name, p.argv()) == ("github", [sys.executable, "-m", "orchestra_bridges", "github"])
    assert [(m.id, m.kind, m.mode) for m in on.machines] == [("bridges.github", "program", "continuous")]


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
    assert [m.id for m in on.machines] == ["bridges.github_prs"]
    [c] = on.collectors
    assert (c.name, c.kind, c.target) == ("github_prs", "scrape", "http://127.0.0.1:9466/metrics")
    assert plugin(proj('[bridges.github_prs]\nrepos = ["a/b"]\nport = 0\n')).collectors == []
    assert plugin(proj("[bridges.github]\n")).collectors == []
