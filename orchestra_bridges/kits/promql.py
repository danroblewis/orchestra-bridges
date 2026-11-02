"""Queries against a Prometheus HTTP API (Prometheus, VictoriaMetrics, Thanos, Mimir, Chronosphere)
through a ``CachedClient``, answered as plain series.

``Prometheus(client, aliases=None, max_series=500)``:

- ``instant(query, at=None)`` and ``range(query, start, end, step)``: ``/api/v1/query`` and
  ``/api/v1/query_range``, POSTed as forms (long selectors never hit URL limits);
- ``series(match, start=None, end=None)``: ``/api/v1/series``, the label sets of what matches,
  **bounded**: each selector must name something (a metric name, or a label matcher that is not
  ``=~".*"``; ``Unbounded`` otherwise), the window is the last hour unless given, and Prometheus is
  asked for at most ``max_series + 1`` (``limit``, which Prometheus 2.49 and later honour; a store
  that ignores it is still refused past ``max_series``). Never ``/api/v1/label/__name__/values``:
  on a large tenant (Chronosphere's) the list of every name is hundreds of megabytes;
- ``schema(match)``: what ``series`` finds, as each metric's name and its label names, for an
  operator choosing the queries;
- each answer is a list of ``Series(name, labels, points)``, points as ``(unix seconds, value)``;
- ``aliases`` renames labels (``{"k8s_cluster": "cluster"}``), so series read from a company's
  store carry the names the project uses;
- ``max_series`` refuses an answer with more series than that (``TooManySeries``): narrow the
  query rather than pull a tenant's cardinality into a machine.

To store what it reads, a machine prints the series for a ``[[collect]]`` entry (``Poller.gauge``);
to have discovery read the store in place, a ``[[sources]]`` entry is the better tool (INTEGRATING.md)."""

from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass, field

from .client import CachedClient, KitError


class PromError(KitError):
    pass


class TooManySeries(PromError):
    pass


class Unbounded(PromError):
    """A series lookup whose selector would match a whole tenant."""


WILD = {"", ".*", ".+", ".*.*", "(.*)", "(.+)"}
_SEL = re.compile(r"^\s*([a-zA-Z_:][a-zA-Z0-9_:]*)?\s*(?:\{(.*)\})?\s*$", re.DOTALL)
_MATCH = re.compile(r'\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*(=~|!~|!=|=)\s*"((?:[^"\\]|\\.)*)"\s*,?')
WINDOW_S = 3600.0


def bounded(selector: str) -> bool:
    """Whether a series selector names something: a metric name, an equality to a value, or a
    regular expression that is not a wildcard. ``{}``, ``{__name__=~".+"}`` and a selector of only
    negative matchers are not."""
    m = _SEL.match(selector or "")
    if not m:
        return False
    if m.group(1):
        return True
    for name, op, value in _MATCH.findall(m.group(2) or ""):
        if op == "=" and value != "":
            return True
        if op == "=~" and value not in WILD:
            return True
    return False


@dataclass
class Series:
    name: str
    labels: dict[str, str] = field(default_factory=dict)
    points: list[tuple[float, float]] = field(default_factory=list)

    @property
    def last(self) -> float | None:
        return self.points[-1][1] if self.points else None


def _answer(body):
    """What is cached of an answer: all of it. A Prometheus answer is names, labels and numbers,
    never an item's text."""
    return body


def _num(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return math.nan


class Prometheus:
    def __init__(self, client: CachedClient, aliases: dict[str, str] | None = None, max_series: int = 500):
        self.client = client
        self.aliases = dict(aliases or {})
        self.max_series = max_series

    def _labels(self, metric: dict) -> tuple[str, dict[str, str]]:
        labels = {self.aliases.get(k, k): str(v) for k, v in (metric or {}).items() if k != "__name__"}
        return str((metric or {}).get("__name__", "")), labels

    def _data(self, path: str, form: dict) -> dict:
        body = self.client.post(path, form=form, keep=_answer)
        if not isinstance(body, dict) or body.get("status") != "success":
            err = body.get("error") if isinstance(body, dict) else None
            raise PromError(f"{path}: {err or 'not a Prometheus answer'}")
        return body.get("data") or {}

    def _series(self, data: dict) -> list[Series]:
        result = data.get("result") or []
        kind = data.get("resultType")
        if kind == "scalar":
            result = [{"metric": {}, "value": result}]
        if len(result) > self.max_series:
            raise TooManySeries(f"{len(result)} series, more than max_series={self.max_series}: narrow the query")
        out = []
        for r in result:
            name, labels = self._labels(r.get("metric"))
            raw = r.get("values") or ([r["value"]] if "value" in r else [])
            out.append(Series(name, labels, [(float(t), _num(v)) for t, v in raw]))
        return out

    def instant(self, query: str, at: float | None = None) -> list[Series]:
        form = {"query": query}
        if at is not None:
            form["time"] = f"{at:.3f}"
        return self._series(self._data("/api/v1/query", form))

    def range(self, query: str, start: float, end: float, step: float) -> list[Series]:
        if step <= 0 or end < start:
            raise ValueError("range needs start <= end and a positive step")
        form = {"query": query, "start": f"{start:.3f}", "end": f"{end:.3f}", "step": f"{step:g}"}
        return self._series(self._data("/api/v1/query_range", form))

    def series(self, match: str | list[str], start: float | None = None, end: float | None = None) -> list[dict[str, str]]:
        """The label sets of what ``match`` selects in the window (the last hour unless given), at
        most ``max_series`` of them: each selector must be ``bounded``."""
        matches = [match] if isinstance(match, str) else list(match)
        if not matches:
            raise Unbounded("a series lookup needs at least one selector")
        for m in matches:
            if not bounded(m):
                raise Unbounded(f"{m!r} would match the whole store: name a metric or a label value "
                                "(the team's job or namespace)")
        end = time.time() if end is None else end
        start = end - WINDOW_S if start is None else start
        form: dict = {"match[]": matches, "start": f"{start:.3f}", "end": f"{end:.3f}",
                      "limit": str(self.max_series + 1)}
        data = self.client.post("/api/v1/series", form=form, keep=_answer)
        if not isinstance(data, dict) or data.get("status") != "success":
            raise PromError("/api/v1/series: not a Prometheus answer")
        rows = data.get("data") or []
        if len(rows) > self.max_series:
            raise TooManySeries(f"more than max_series={self.max_series} series: narrow the match")
        out = []
        for m in rows:
            name, labels = self._labels(m)
            out.append({"__name__": name, **labels})
        return out

    def schema(self, match: str | list[str], start: float | None = None, end: float | None = None) -> dict[str, list[str]]:
        """Each metric ``match`` selects and its label names (aliases applied), sorted: what an
        operator reads to choose a query, without reading any value."""
        out: dict[str, set[str]] = {}
        for row in self.series(match, start, end):
            out.setdefault(row["__name__"], set()).update(k for k in row if k != "__name__")
        return {name: sorted(keys) for name, keys in sorted(out.items())}
