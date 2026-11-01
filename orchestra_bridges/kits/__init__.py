"""Kits for pollers (relays of what people and bots did) that bring an outside system in (Orchestra's docs/INTEGRATING.md):
small parts a coding tool or the Supervisor builds a poller from, and a scaffold that writes one.

- ``client.CachedClient``: HTTP with a disk TTL cache, a per-host rate limit, retries, the token
  kept in memory, a dry run;
- ``promql.Prometheus``: instant, range and series queries against a Prometheus API, as plain series;
- ``poller.Poller``: the interval loop, the cursor, idempotent emission, the series a
  ``[[collect]]`` entry reads, the announcement, a clean stop;
- ``fakes``: HTTP fakes of GitHub, Jira (Data Center and Cloud), Slack, PagerDuty and a Prometheus
  API for tests;
- ``scaffold``: ``orchestra bridges scaffold <kind> <name> [--root repo|state|overlay]``, the
  repository's ``.orchestra/machines/`` by default;
- ``notes``: what a machine says of itself on its card (``fake``, ``stand_in``, ``disabled``);
- ``settings``: ``[bridges.kits]``; ``machinefile``: a machine's ``machine.toml`` and its clearance;
- ``testing``: what a scaffolded test reads from its ``machine.toml`` (the token's variable, the scope);
- ``integrations``: ``orchestra bridges integrations``, the scaffolded machines and their clearance.

A poller built from a kit is a relay, never a machine (Orchestra's rule 2, D19; the bridges' SPEC §2):
it publishes only as the people and bots who acted (``person.<system>.<account>``, ``bot.<system>``),
announces each of them once, and never announces itself or publishes under its own name. It keeps
its cursor, the ids it emitted and a cache of only what it kept in the state directory (SPEC §8)."""

from .client import CachedClient, HTTPFailure, KitError, NotCached, TokenMissing
from .poller import Poller
from .promql import PromError, Prometheus, Series, TooManySeries

__all__ = ["CachedClient", "HTTPFailure", "KitError", "NotCached", "Poller", "PromError", "Prometheus", "Series",
           "TokenMissing", "TooManySeries"]
