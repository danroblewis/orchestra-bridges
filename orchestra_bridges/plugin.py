"""The bridges as an Orchestra plugin (``orchestra/plugins.py``): for each bridge configured in the
project's ``orchestra.toml`` (``[bridges.github]``, ``[bridges.slack]``), the process ``orchestra run`` keeps running
(named ``bridges.<system>`` there and in its log's name) and a placeholder for the machine it is, so
the Machines page lists it before its first event (it is a machine by the events it publishes, not
by this declaration). The people it publishes for are known only once seen.

For the Slack bridge it also declares the instrument that stores its counts: a scrape of its
``/metrics`` (``[bridges.slack] host`` and ``port``), which Orchestra runs as ``bridges.slack`` and
whose points it labels ``instrument="bridges.slack"``. The bridge reads its counters back by that
label when it restarts (``slack/bridge.py``). A project's own ``[[scrape]]`` named ``bridges.slack``
takes its place. With ``port = 0`` (any free port) there is nothing to point a scrape at, so none is
declared. No pages: what the bridges show is Orchestra's own Machines and Instruments pages and
series."""

from __future__ import annotations

import ipaddress
import sys

from orchestra.collect.spec import Collector
from orchestra.launcher import Process
from orchestra.plugins import Declared, Plugin

from .config import section

SYSTEMS = ("github", "slack")
SCRAPE_EVERY_S = 30.0


def metrics_url(slack: dict) -> str | None:
    """Where the Slack bridge serves ``/metrics``, as ``[bridges.slack]`` says; None when its port
    is not a fixed one. A wildcard host (``0.0.0.0``, ``::``) is scraped on the loopback address."""
    host, port = str(slack.get("host") or "127.0.0.1"), slack.get("port", 9465)
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
    configured = [s for s in SYSTEMS if section(raw, s) is not None]
    slack = section(raw, "slack")
    url = metrics_url(slack) if slack is not None else None
    return Plugin(
        "bridges",
        machines=[Declared(f"bridges.{s}", "program", "continuous") for s in configured],
        processes=[Process(s, [sys.executable, "-m", "orchestra_bridges", s], stop_grace_s=10) for s in configured],
        collectors=[Collector.scrape("slack", url, every_s=SCRAPE_EVERY_S)] if url else [])
