"""The operator's guide (``orchestra_bridges/guide.md``) ships in the package, is the kits' guide (no
retired reader taught as current), names every kit, and every TOML example in it is configuration
the plugin and the scaffold accept as written."""

import re
import tomllib
from pathlib import Path

import pytest
from orchestra.config_edit import problems

import orchestra_bridges
from orchestra_bridges.check import check
from orchestra_bridges.kits.scaffold import KINDS, entries

GUIDE = Path(orchestra_bridges.__file__).parent / "guide.md"
TEXT = GUIDE.read_text(encoding="utf-8")


def blocks() -> list[str]:
    return re.findall(r"^```toml\n(.*?)^```", TEXT, re.MULTILINE | re.DOTALL)


def test_the_guide_is_in_the_package_and_names_every_kit():
    assert GUIDE.is_file() and len(blocks()) >= 2
    for kind in KINDS:
        assert f"| `{kind}` |" in TEXT, kind
    for word in ("approve_integration", "integrate_system", ".orchestra/machines/<name>/", "Outbox", "clearance",
                 "token_file", "edition", "HTTPS_PROXY", "ca_file"):
        assert word in TEXT, word


def test_the_retired_readers_are_named_only_as_retired():
    for section in ("[bridges.github]", "[bridges.jira]", "[bridges.slack]"):
        lines = [ln for ln in TEXT.splitlines() if section in ln]
        assert lines and all("retired" in ln or "retired" in TEXT[TEXT.index(ln) - 300:TEXT.index(ln) + 300]
                             for ln in lines), section
    assert "bridges.<section>" not in TEXT


@pytest.mark.parametrize("n", range(len(blocks())))
def test_every_toml_example_is_accepted(n):
    raw = tomllib.loads(blocks()[n])
    if "people" in raw:                       # the identity map, a file of its own
        from orchestra_bridges.identity import People
        assert People(raw["people"]).listed("github", "alice")
        return
    if "bridges" in raw:
        assert check(raw) == [] and problems(raw, check) == []
    else:                                   # the two entries approve_integration writes, as the scaffold prints them
        assert {"machines", "collect"} <= set(raw)
        printed = tomllib.loads(entries("jira_poll", ".orchestra/machines/jira_poll"))
        assert printed == raw
