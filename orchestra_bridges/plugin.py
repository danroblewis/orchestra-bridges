"""The bridges as an Orchestra plugin (``orchestra/plugins.py``): a library, never a reader.

The plugin reads no system itself (the owner, 2026-11-01: "a library or framework so that AI agents
within my repo ... could write scripts in the target repo to do the metrics and event
collection"). What it provides is the kits (``kits/``: the client, the poller, the runner, the
fakes, the templates and the scaffold), the clearance gate every scaffolded script runs, ``orchestra
bridges scaffold`` and ``orchestra bridges integrations`` (``__main__.py``), the operator's guide
(``guide.md``, served to the Supervisor through ``/api/guides``) and the check of its one section,
``[bridges.kits]``.

**No processes, no collectors, no machines.** A collector built from a kit lives in the owner's
repository (``.orchestra/machines/<name>/``, or the state directory when asked) and runs from the
project's own configuration: a ``[[machines]]`` entry keeps its poller running (a launcher process,
known only as a process: its actors are the machines) and a ``[[collect]]`` entry stores its
measurement (an instrument). The Supervisor's ``approve_integration`` writes both once the owner
approved. Nothing here needs declaring for that, and the plugin declares no machine: the people and
bots a poller relays are known once seen."""

from __future__ import annotations

from pathlib import Path

from orchestra.plugins import Plugin


def plugin(project) -> Plugin:
    return Plugin(
        "bridges",
        guide=Path(__file__).parent / "guide.md",   # how to set up a collector, for an AI operator (/api/guides)
        check=_check)                                # what project.config_edit refuses in [bridges]


def _check(raw: dict, repo_root: Path | None) -> list[str]:
    """``Plugin.check``: the bridges' own check (``check.py``)."""
    from .check import check

    return check(raw)
