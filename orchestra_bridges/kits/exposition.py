"""Series in the Prometheus text format, as a ``[[collect]]`` entry reads them (docs/machines.md,
"What a collector prints").

A poller keeps its counters here (saved with its cursor after each good poll); a scaffolded
program's ``measure`` fills a ``Registry`` and writes it to standard output (``write("-")``) for its
``[[collect]]`` entry. The machine does not store them: an instrument does."""

from __future__ import annotations

import math
import re
import sys

NAME = re.compile(r"^[a-zA-Z_:][a-zA-Z0-9_:]*$")
LABEL = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")


def _key(labels: dict[str, str] | None) -> tuple:
    return tuple(sorted((labels or {}).items()))


def _value(v: float) -> str:
    if math.isnan(v):
        return "NaN"
    if math.isinf(v):
        return "+Inf" if v > 0 else "-Inf"
    return repr(float(v)) if not float(v).is_integer() else str(int(v))


def _escape(v: str) -> str:
    return v.replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


class Registry:
    def __init__(self):
        self.gauges: dict[str, dict[tuple, float]] = {}
        self.counters: dict[str, dict[tuple, float]] = {}
        self.help: dict[str, str] = {}

    @staticmethod
    def _check(name: str, labels: dict[str, str] | None) -> None:
        if not NAME.match(name):
            raise ValueError(f"{name!r} is not a series name")
        for k in labels or {}:
            if not LABEL.match(k) or k.startswith("__"):
                raise ValueError(f"{k!r} is not a label name")

    def gauge(self, name: str, value: float, labels: dict[str, str] | None = None, help: str | None = None) -> None:
        self._check(name, labels)
        self.gauges.setdefault(name, {})[_key(labels)] = float(value)
        if help:
            self.help[name] = help

    def counter(self, name: str, by: float = 1.0, labels: dict[str, str] | None = None, help: str | None = None) -> None:
        self._check(name, labels)
        if by < 0:
            raise ValueError("a counter only goes up")
        series = self.counters.setdefault(name, {})
        series[_key(labels)] = series.get(_key(labels), 0.0) + float(by)
        if help:
            self.help[name] = help

    def render(self) -> str:
        out: list[str] = []
        for kind, table in (("gauge", self.gauges), ("counter", self.counters)):
            for name in sorted(table):
                if name in self.help:
                    out.append(f"# HELP {name} {self.help[name]}")
                out.append(f"# TYPE {name} {kind}")
                for labels, v in sorted(table[name].items()):
                    ls = ",".join(f'{k}="{_escape(str(x))}"' for k, x in labels)
                    out.append(f"{name}{{{ls}}} {_value(v)}" if ls else f"{name} {_value(v)}")
        return "\n".join(out) + ("\n" if out else "")

    def counters_state(self) -> dict:
        return {n: [[list(map(list, k)), v] for k, v in t.items()] for n, t in self.counters.items()}

    def load_counters(self, state: dict) -> None:
        for n, rows in (state or {}).items():
            self.counters[n] = {tuple(tuple(p) for p in k): float(v) for k, v in rows}

    def write(self, path: str = "-") -> None:
        """Print the series on standard output (``-``), for a ``[[collect]]`` entry."""
        if path != "-":
            raise ValueError("series are printed on standard output, for the instrument that runs measure")
        sys.stdout.write(self.render())
        sys.stdout.flush()

