"""Kits for Programmatic Machines that bring an outside system in (Orchestra's docs/INTEGRATING.md):
small parts a coding tool or the Supervisor builds a poller from, and a scaffold that writes one.

- ``client.CachedClient``: HTTP with a disk TTL cache, a per-host rate limit, retries, the token
  kept in memory, a dry run;
- ``promql.Prometheus``: instant, range and series queries against a Prometheus API, as plain series;
- ``poller.Poller``: the interval loop, the cursor, idempotent emission, the series a
  ``[[collect]]`` entry reads, the announcement, a clean stop;
- ``fakes``: HTTP fakes of Jira, Slack, PagerDuty and a Prometheus API for tests;
- ``scaffold``: ``orchestra bridges scaffold <kind> <name> [--root state|overlay|repo]``;
- ``settings``: ``[bridges.kits]``; ``machinefile``: a machine's ``machine.toml`` and its clearance.

A machine built from a kit is a machine (it publishes under its own name), never a bridge (a relay
for people and bots, SPEC §2): the bridges' relay rule does not apply to it; the rest do (SPEC §8)."""

from .client import CachedClient, HTTPFailure, KitError, NotCached, TokenMissing
from .poller import Poller
from .promql import PromError, Prometheus, Series, TooManySeries

__all__ = ["CachedClient", "HTTPFailure", "KitError", "NotCached", "Poller", "PromError", "Prometheus", "Series",
           "TokenMissing", "TooManySeries"]
