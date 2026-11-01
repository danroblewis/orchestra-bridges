"""A fake of the Prometheus HTTP query API (what Chronosphere, Mimir, Thanos and VictoriaMetrics
answer too): ``/api/v1/query``, ``/api/v1/query_range`` and ``/api/v1/series``, by GET or a POSTed
form, in the real shapes (``{"status": "success", "data": {"resultType", "result"}}``).

It evaluates a small PromQL: a selector ``name{label="v", label!="v", label=~"re", label!~"re"}``,
optionally inside ``sum(...)`` or ``sum by (a, b) (...)``. Anything else is a 400 ``bad_data``, as
Prometheus answers what it cannot parse. A sample counts at a time when it is the last at or before
it within the lookback (5 minutes). Fixture times are seconds relative to the fake's ``t0``."""

from __future__ import annotations

import re
import time
from typing import Any

from .base import FakeServer, one

LOOKBACK_S = 300.0
SELECTOR = re.compile(r"^\s*([a-zA-Z_:][a-zA-Z0-9_:]*)?\s*(\{(.*)\})?\s*$")
MATCHER = re.compile(r'\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*(=~|!~|!=|=)\s*"((?:[^"\\]|\\.)*)"\s*')
SUM = re.compile(r"^\s*sum\s*(?:by\s*\(([^)]*)\)\s*)?\((.*)\)\s*$")


class BadQuery(ValueError):
    pass


def parse_selector(s: str):
    m = SELECTOR.match(s)
    if not m or not (m.group(1) or m.group(3)):
        raise BadQuery(f"parse error: unexpected {s.strip()[:40]!r}")
    matchers = [("__name__", "=", m.group(1))] if m.group(1) else []
    body = m.group(3) or ""
    pos = 0
    while pos < len(body):
        mm = MATCHER.match(body, pos)
        if not mm:
            raise BadQuery(f"parse error: bad matcher in {body!r}")
        matchers.append((mm.group(1), mm.group(2), mm.group(3)))
        pos = mm.end()
        if pos < len(body):
            if body[pos] != ",":
                raise BadQuery(f"parse error: expected , in {body!r}")
            pos += 1

    def matches(labels: dict) -> bool:
        for k, op, v in matchers:
            got = labels.get(k, "")
            ok = {"=": got == v, "!=": got != v, "=~": re.fullmatch(v, got) is not None,
                  "!~": re.fullmatch(v, got) is None}[op]
            if not ok:
                return False
        return True

    return matches


def parse(query: str):
    """``(matches, by)``: by is None for no aggregation, else the list of labels summed by."""
    if m := SUM.match(query):
        by = [x.strip() for x in (m.group(1) or "").split(",") if x.strip()]
        return parse_selector(m.group(2)), by
    return parse_selector(query), None


class FakePrometheus(FakeServer):
    fixture_name = "prometheus.json"

    def __init__(self, fixture=None, token: str | None = None, t0: float | None = None):
        super().__init__(fixture, token)
        self.t0 = time.time() if t0 is None else t0

    def add_sample(self, labels: dict[str, str], at: float, value: float) -> None:
        """A sample at ``at`` seconds relative to ``t0``."""
        with self._lock:
            for s in self.data["series"]:
                if s["labels"] == labels:
                    s["samples"].append([at, value])
                    s["samples"].sort()
                    return
            self.data["series"].append({"labels": dict(labels), "samples": [[at, value]]})

    def _at(self, s: dict, t: float) -> float | None:
        best = None
        for rel, v in s["samples"]:
            ts = self.t0 + rel
            if t - LOOKBACK_S < ts <= t + 1e-9:
                best = v
        return best

    def _eval(self, query: str, t: float) -> list[tuple[dict, float]]:
        matches, by = parse(query)
        rows = [(s["labels"], v) for s in self.data["series"] if matches(s["labels"]) and (v := self._at(s, t)) is not None]
        if by is None:
            return rows
        groups: dict[tuple, float] = {}
        for labels, v in rows:
            k = tuple((b, labels.get(b, "")) for b in by if labels.get(b))
            groups[k] = groups.get(k, 0.0) + v
        return [(dict(k), v) for k, v in groups.items()]

    def route(self, method: str, path: str, query: dict[str, list[str]], body: Any) -> tuple[int, Any]:
        try:
            if path == "/api/v1/query":
                t = float(one(query, "time") or time.time())
                rows = self._eval(one(query, "query") or "", t)
                return 200, {"status": "success", "data": {"resultType": "vector", "result": [
                    {"metric": labels, "value": [t, _s(v)]} for labels, v in rows]}}
            if path == "/api/v1/query_range":
                q = one(query, "query") or ""
                start, end, step = float(one(query, "start")), float(one(query, "end")), float(one(query, "step"))
                if step <= 0 or end < start:
                    raise BadQuery("end timestamp must not be before start time, and step positive")
                if (end - start) / step > 11000:
                    raise BadQuery("exceeded maximum resolution of 11,000 points per timeseries")
                series: dict[tuple, dict] = {}
                n = 0
                while start + n * step <= end + 1e-9:
                    t = start + n * step
                    for labels, v in self._eval(q, t):
                        k = tuple(sorted(labels.items()))
                        series.setdefault(k, {"metric": labels, "values": []})["values"].append([t, _s(v)])
                    n += 1
                return 200, {"status": "success", "data": {"resultType": "matrix", "result": list(series.values())}}
            if path == "/api/v1/series":
                ms = [parse_selector(m) for m in query.get("match[]") or []]
                if not ms:
                    raise BadQuery("no match[] parameter provided")
                return 200, {"status": "success", "data": [s["labels"] for s in self.data["series"]
                                                           if any(m(s["labels"]) for m in ms)]}
        except (BadQuery, TypeError, ValueError) as e:
            return 400, {"status": "error", "errorType": "bad_data", "error": str(e)}
        return 404, {"status": "error", "errorType": "not_found", "error": f"no endpoint {path}"}


def _s(v: float) -> str:
    return str(int(v)) if float(v).is_integer() else repr(float(v))
