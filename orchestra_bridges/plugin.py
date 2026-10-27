"""The bridges as an Orchestra plugin (``orchestra/plugins.py``): for each bridge configured in the
project's ``orchestra.toml`` (``[bridges.github]``), the process ``orchestra run`` keeps running
(named ``bridges.github`` there and in its log's name) and the machine it is, declared so the
Machines page lists it before its first event. The people it publishes for are known only once
seen. No pages: what the bridges show is Orchestra's own Machines page and series."""

from __future__ import annotations

import sys

from orchestra.launcher import Process
from orchestra.plugins import Declared, Plugin

from .config import section
from .github.bridge import MACHINE


def plugin(project) -> Plugin:
    configured = section(project.config.raw, "github") is not None
    return Plugin(
        "bridges",
        machines=[Declared(MACHINE, "program", "continuous")] if configured else [],
        processes=[Process("github", [sys.executable, "-m", "orchestra_bridges", "github"], stop_grace_s=10)]
        if configured else [])
