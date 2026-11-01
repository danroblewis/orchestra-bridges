"""chronosphere_series: series read from a Prometheus API (Prometheus, Chronosphere, Mimir, Thanos,
VictoriaMetrics), printed for a ``[[collect]]`` entry (scaffolded from the orchestra-bridges kit
``promql``; edit freely, AGENTS.md says what it may do).

A metrics store has no item events, so this has no poller: ``python chronosphere_series.py measure`` runs each
query in ``[settings] queries`` (instant, at now) and prints one gauge per answer series, named by
the query's ``name``, with the answer's labels (``aliases`` applied). Prefer a ``[[sources]]`` entry,
which Orchestra reads in place at each discovery with its own limits; use this when a series must be
computed (a ratio, a sum over a tenant's cardinality) or stored here.

``max_series`` guards each query: an answer with more series than that fails rather than flood the
store. Only names, labels and numbers are read: a store holds no item's text."""

from __future__ import annotations

import math
import sys
from pathlib import Path

from orchestra_bridges.kits import runner
from orchestra_bridges.kits.promql import Prometheus

HERE = Path(__file__).resolve().parent


def make_poll(api, settings):
    def poll(p):
        p.stop()            # nothing to relay: run `measure` from a [[collect]] entry
    return poll


def measure(api, settings, series) -> None:
    prom = Prometheus(api, aliases=settings.extra.get("aliases") or {},
                      max_series=int(settings.extra.get("max_series", 200)))
    for q in settings.extra.get("queries") or []:
        for s in prom.instant(q["query"]):
            if s.last is not None and not math.isnan(s.last):
                series.gauge(q["name"], s.last, s.labels, help=q.get("help"))


def main(argv: list[str] | None = None) -> int:
    return runner.main(HERE, make_poll, argv, measure=measure)


if __name__ == "__main__":
    sys.exit(main())
