"""``examples/integrations/scaffolded/`` is exactly what the scaffold writes for each kit (with
``--root repo``: paths named from the repository's root), committed as the reference a Supervisor
or a reviewer reads. Their own tests run in this suite against the fakes. After changing a
template: ``ORCHESTRA_BRIDGES_REGENERATE=1 uv run python -m pytest tests/unit/test_scaffolded_examples.py``."""

import filecmp
import os
import shutil
from pathlib import Path

import pytest

from orchestra_bridges.kits.scaffold import KINDS, scaffold

REFERENCE = Path(__file__).resolve().parents[2] / "examples" / "integrations" / "scaffolded"


@pytest.mark.parametrize("kind", sorted(KINDS))
def test_the_reference_is_what_the_scaffold_writes(kind, tmp_path):
    name = f"{kind}_example"
    fresh = scaffold(kind, name, tmp_path, ".orchestra")
    committed = REFERENCE / "machines" / name
    if os.environ.get("ORCHESTRA_BRIDGES_REGENERATE"):
        shutil.rmtree(committed, ignore_errors=True)
        shutil.copytree(fresh, committed)
    files = sorted(p.name for p in fresh.iterdir())
    assert sorted(p.name for p in committed.iterdir() if p.name != "__pycache__") == files
    _, mismatch, errors = filecmp.cmpfiles(fresh, committed, files, shallow=False)
    assert not mismatch and not errors, f"{mismatch or errors} differ: regenerate (this file's docstring)"
