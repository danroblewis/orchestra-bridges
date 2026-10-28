"""The operator's guide (``orchestra_bridges/guide.md``) ships in the package, names every setting
the bridges read, and every TOML example in it is configuration the bridges accept as written."""

import re
import tomllib
from pathlib import Path

import pytest
from orchestra.config_edit import problems
from orchestra.sources import spec as sources

import orchestra_bridges
from orchestra_bridges.check import KEYS, check
from orchestra_bridges.identity import People

PACKAGE = Path(orchestra_bridges.__file__).parent
GUIDE = PACKAGE / "guide.md"
PRESET = PACKAGE / "rabbitmq" / "preset.toml"


def blocks() -> list[str]:
    return re.findall(r"^```toml\n(.*?)^```", GUIDE.read_text(encoding="utf-8"), re.MULTILINE | re.DOTALL)


def test_the_guide_is_in_the_package():
    assert GUIDE.is_file() and GUIDE.stat().st_size > 1000
    assert len(blocks()) >= 8


@pytest.mark.parametrize("kind", sorted(KEYS))
def test_the_guide_names_every_setting(kind):
    text = GUIDE.read_text(encoding="utf-8")
    missing = [k for k in sorted(KEYS[kind]) if f"`{k}`" not in text and not re.search(rf"^{k} =", text, re.MULTILINE)]
    assert not missing, f"{kind}: the guide never names {missing}"


@pytest.mark.parametrize("n", range(len(blocks())))
def test_every_toml_example_is_accepted(n, tmp_path):
    text = blocks()[n]
    raw = tomllib.loads(text)
    if "people" in raw:                                   # the identity map, a file of its own
        p = People(raw["people"])
        assert all(p.accounts(s) for s in ("github", "jira", "slack"))
        return
    if "sources" in raw:                                  # Orchestra's [[sources]], with the preset this package ships
        for s in raw["sources"]:
            assert s["preset"].endswith("orchestra_bridges/rabbitmq/preset.toml")
            s["preset"] = str(PRESET)
        assert sources.load(raw, tmp_path).refused == {}
        return
    assert "bridges" in raw
    assert check(raw, tmp_path) == []
    assert problems(raw, check) == []


@pytest.mark.parametrize(("toml", "says"), [
    ('[bridges.github]\nrepos = ["a/b"]\ninterval = "1m"\n', "has no setting interval"),
    ('[bridges.github]\nrepos = ["a/b"]\ntoken = "x"\n', "a token never goes in orchestra.toml"),
    ('[bridges.gitlab]\nrepos = ["a/b"]\n', "is no bridge"),
    ('[bridges.github]\nrepos = ["a/b"]\n[bridges.github_prs]\nrepos = ["a/b"]\nport = 9464\n', "port 9464"),
    ('[bridges.jira]\napi = "https://jira.acme.com"\n', "needs either projects or jql"),
    ('[bridges.jira_two]\napi = "https://jira.acme.com"\nprojects = ["AB"]\n', "port is required"),
    ('[bridges]\npeople = 3\n', "people must be"),
])
def test_the_check_says_what_would_break_a_bridge(toml, says):
    said = check(tomllib.loads(toml))
    assert any(says in s for s in said), said
    assert any(says in s for s in problems(tomllib.loads(toml), check))


def test_the_check_is_quiet_without_bridges_and_for_good_settings():
    assert check({}) == []
    assert check(tomllib.loads('[bridges.github]\nrepos = ["a/b"]\n[bridges.slack]\n[bridges.jira]\n'
                               'api = "https://acme.atlassian.net"\nprojects = ["AB"]\nemail = "me@acme.com"\n')) == []
