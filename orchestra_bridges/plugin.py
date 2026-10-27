"""The bridges as an Orchestra plugin (``orchestra/plugins.py``): for each bridge configured in the
project's ``orchestra.toml`` (``[bridges.github]``, ``[bridges.github_prs]``, ``[bridges.jira]`` and
any second Jira site, ``[bridges.jira_<name>]``), the process
``orchestra run`` keeps running (named ``bridges.<system>`` there and in its log's name) and a
placeholder for the machine it is, so the Machines page lists it before its first event (it is a
machine by the events it publishes, not by this declaration). The people it publishes for are known
only once seen.

For the pull-request and Jira bridges it also declares the instrument that stores what each
measures: a scrape of its ``/metrics`` (the section's ``host`` and ``port``), which Orchestra runs as
``bridges.<section>`` and whose points it labels ``instrument="bridges.<section>"``. The bridge
reads its last good poll back by that label when it restarts. A project's own ``[[scrape]]`` of the
same name takes its place. With ``port = 0`` (any free port) there is nothing to point a
scrape at, so none is declared. No pages: what the bridges show is Orchestra's own Machines and
Instruments pages and series."""

from __future__ import annotations

import ipaddress
import sys

from orchestra.collect.spec import Collector
from orchestra.launcher import Process
from orchestra.plugins import Declared, Plugin

from .config import JIRA_PORT, jira_sections, section

SYSTEMS = ("github", "github_prs")
SCRAPED = {"github_prs": 9466, "jira": JIRA_PORT}   # the bridges that serve /metrics, and their default port
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
    scrapes = [(s, url) for s in configured if (s in SCRAPED or s.startswith("jira_"))
               and (url := metrics_url(section(raw, s), SCRAPED.get(s)))]
    return Plugin(
        "bridges",
        machines=[Declared(f"bridges.{s}", "program", "continuous") for s in configured],
        processes=[Process(s, [sys.executable, "-m", "orchestra_bridges", s], stop_grace_s=10) for s in configured],
        collectors=[Collector.scrape(s, url, every_s=SCRAPE_EVERY_S) for s, url in scrapes])
