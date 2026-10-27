"""The bridges as an Orchestra plugin (``orchestra/plugins.py``): for each bridge configured in the
project's ``orchestra.toml`` (``[bridges.github]``, ``[bridges.slack]``), the process ``orchestra run`` keeps running
(named ``bridges.<system>`` there and in its log's name) and the machine it is, declared so the
Machines page lists it before its first event. The people it publishes for are known only once
seen. No pages: what the bridges show is Orchestra's own Machines page and series."""

from __future__ import annotations

import sys

from orchestra.launcher import Process
from orchestra.plugins import Declared, Plugin

from .config import section

SYSTEMS = ("github", "slack")


def plugin(project) -> Plugin:
    configured = [s for s in SYSTEMS if section(project.config.raw, s) is not None]
    return Plugin(
        "bridges",
        machines=[Declared(f"bridges.{s}", "program", "continuous") for s in configured],
        processes=[Process(s, [sys.executable, "-m", "orchestra_bridges", s], stop_grace_s=10) for s in configured])
