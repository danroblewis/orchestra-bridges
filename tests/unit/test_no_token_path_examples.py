"""The guide, the kit templates, the examples, the README and the SPEC name no token path as an
example: the owner names the file that holds a token, and the Supervisor, which reads these, never
suggests one (bootstrap run 6 offered a path that existed on the owner's machine). The settings of a
running project are not in this repository."""

from pathlib import Path

import pytest

import orchestra_bridges

PACKAGE = Path(orchestra_bridges.__file__).parent
ROOT = PACKAGE.parent
FILES = sorted(
    [PACKAGE / "guide.md", ROOT / "README.md", ROOT / "SPEC.md", PACKAGE / "kits" / "settings.py"]
    + [p for p in (PACKAGE / "kits" / "templates").rglob("*") if p.is_file()]
    + [p for p in (ROOT / "examples").rglob("*") if p.is_file() and p.suffix in {".toml", ".md", ".py"}]
)


@pytest.mark.parametrize("path", FILES, ids=lambda p: str(p.relative_to(ROOT)))
def test_no_example_names_a_token_path(path):
    text = path.read_text(encoding="utf-8", errors="replace")
    assert ".config/orchestra-bridges/" not in text


def test_the_guide_says_the_owner_names_the_token_file():
    text = (PACKAGE / "guide.md").read_text(encoding="utf-8")
    assert "the owner names the file that holds a token" in text
    assert 'token_file = "<path to the token file>"' in text
