"""The plugin is a library: it declares no process, no instrument and no machine, only its guide and
the check of ``[bridges.kits]``; the retired readers' sections are refused with what replaced them;
the command line knows only ``scaffold`` and ``integrations``."""

from pathlib import Path

import pytest
from orchestra.config import Config, ConfigError, Project

from orchestra_bridges.__main__ import main as bridges_main
from orchestra_bridges.check import check
from orchestra_bridges.plugin import plugin


def proj(toml: str) -> Project:
    return Project("k", Path("/repo"), Config.from_toml(toml))


def test_the_plugin_declares_no_process_instrument_or_machine():
    for toml in ("[events]\nurl = 'file'\n", '[bridges.kits]\nttl = "2m"\n'):
        p = plugin(proj(toml))
        assert (p.name, p.processes, p.collectors, p.machines, p.pages) == ("bridges", [], [], [], [])
        assert p.guide.is_file() and p.guide.name == "guide.md"
        assert p.check is not None


def test_orchestra_finds_the_installed_plugin():
    from orchestra.plugins import load

    loaded = load(proj('[bridges.kits]\nttl = "2m"\n'))
    assert "bridges" in [p.name for p in loaded.plugins] and not loaded.errors


@pytest.mark.parametrize("section,kit", [("github", "github"), ("github_prs", "github"), ("slack", "slack"),
                                         ("jira", "jira"), ("jira_fedramp", "jira")])
def test_a_retired_readers_section_is_refused_with_its_kit(section, kit):
    [msg] = check({"bridges": {section: {"every": "60s"}}})
    assert "retired" in msg and f"orchestra bridges scaffold {kit} <name>" in msg


def test_the_check():
    assert check({}) == [] and check({"bridges": {"kits": {"ttl": "2m"}}}) == []
    assert check({"bridges": {"people": "people.toml"}}) == []                # the identity map stays
    assert check({"bridges": {"people": 3}}) == ["[bridges] people must be the path of the identity map (a string)"]
    assert "no setting" in check({"bridges": {"other": {}}})[0]
    assert check({"bridges": {"kits": {"token": "x"}}})
    assert check({"bridges": 3}) == ["[bridges] must be a table"]


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
    with pytest.raises(ConfigError, match=r"bridges: \[bridges.github\] was retired"):
        project.config_edit.set("bridges.github.repos", ["acme/service"])
    c = project.config_edit.set("bridges.kits.ttl", "2m")
    assert c.changed and not c.warnings
    with pytest.raises(ConfigError, match=r"bridges: \[bridges.kits\] has no setting interval"):
        project.config_edit.set("bridges.kits.interval", "1m")


def test_the_command_line_knows_only_the_kits(capsys):
    assert bridges_main(["github"]) == 2
    err = capsys.readouterr().err
    assert "retired" in err and "scaffold" in err
    assert bridges_main(["nope"]) == 2 and "retired" not in capsys.readouterr().err
