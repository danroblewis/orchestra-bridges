"""The bridges as an Orchestra plugin (``orchestra/plugins.py``): for each bridge configured in the
project's ``orchestra.toml`` (``[bridges.github]``, ``[bridges.github_prs]``, ``[bridges.slack]``,
``[bridges.jira]`` and any second Jira site, ``[bridges.jira_<name>]``), the process ``orchestra
run`` keeps running (named ``bridges.<section>`` there and in its log's name), and the instrument that stores what it
measures.

**No machines.** A bridge is a relay, never a machine (Orchestra's AGENTS.md rule 2, D19): the
machines are the people and bots it publishes as, known only once seen. So nothing is declared
for the Machines page.

**The instrument** is a scrape of the bridge's ``/metrics`` (the section's ``host`` and ``port``),
which Orchestra runs as ``bridges.<section>`` and whose points it labels
``instrument="bridges.<section>"``. The bridge reads its last good poll and its counters back by
that label when it restarts. A project's own ``[[scrape]]`` of the same name takes its place. With
``port = 0`` (any free port) there is nothing to point a scrape at, so none is declared. No pages:
what the bridges show is Orchestra's own Machines and Instruments pages and series."""

from __future__ import annotations

import ipaddress
import sys
from pathlib import Path

from orchestra.collect.spec import Collector
from orchestra.launcher import Process
from orchestra.plugins import Plugin

from .config import GITHUB_PORT, JIRA_PORT, jira_sections, section

SYSTEMS = ("github", "github_prs", "slack")
PORTS = {"github": GITHUB_PORT, "github_prs": 9466, "slack": 9465, "jira": JIRA_PORT}   # each bridge's default /metrics port
SCRAPE_EVERY_S = 30.0


def metrics_url(conf: dict, default_port: int | None) -> str | None:
    """Where a bridge serves ``/metrics``, as its section says; None when its port is not a fixed
    one. A wildcard host (``0.0.0.0``, ``::``) is scraped on the loopback address."""
    host, port = str(conf.get("host") or "127.0.0.1"), conf.get("port", default_port)
    if isinstance(port, bool) or not isinstance(port, int) or not 0 < port < 65536:
        return None
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return f"http://{host}:{port}/metrics"
    if ip.is_unspecified:
        ip = ipaddress.ip_address("::1" if ip.version == 6 else "127.0.0.1")
    return f"http://{f'[{ip}]' if ip.version == 6 else ip}:{port}/metrics"


def plugin(project) -> Plugin:
    raw = project.config.raw
    configured = [s for s in SYSTEMS if section(raw, s) is not None] + jira_sections(raw)
    scrapes = [(s, url) for s in configured if (url := metrics_url(section(raw, s), PORTS.get(s)))]
    return Plugin(
        "bridges",
        processes=[Process(s, [sys.executable, "-m", "orchestra_bridges", s], stop_grace_s=10) for s in configured],
        collectors=[Collector.scrape(s, url, every_s=SCRAPE_EVERY_S) for s, url in scrapes],
        guide=Path(__file__).parent / "guide.md")   # how to configure it, for an AI operator (/api/guides)
