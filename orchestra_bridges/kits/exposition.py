"""Series in the Prometheus text format, as a ``[[collect]]`` entry reads them (docs/machines.md,
"What a collector prints").

A poller keeps its gauges and counters here and writes them whole, after each good poll, to a file
(renamed into place, so a reader never sees half) or to standard output. The machine does not
store them: an instrument does. The ``[[collect]]`` entry that adopts the file runs

    python -m orchestra_bridges.kits.exposition <file> --max-age 900

which prints the file when it was written within ``--max-age`` seconds, and otherwise prints
nothing and exits 1, so a poller that stopped shows on the Instruments page as an instrument that
fails, never as a level that holds still."""

from __future__ import annotations

import argparse
import math
import os
import re
import sys
import time
from pathlib import Path

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

    def clear_gauges(self) -> None:
        self.gauges.clear()

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

    def write(self, path: str | Path) -> None:
        text = self.render()
        if str(path) == "-":
            sys.stdout.write(text)
            sys.stdout.flush()
            return
        p = Path(path)
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(f".{os.getpid()}.tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, p)


def show(path: str | Path, max_age_s: float, now: float | None = None) -> str | None:
    """The file's text when it was written within ``max_age_s``; else None."""
    p = Path(path)
    try:
        if (now or time.time()) - p.stat().st_mtime > max_age_s:
            return None
        return p.read_text(encoding="utf-8")
    except OSError:
        return None


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="python -m orchestra_bridges.kits.exposition")
    ap.add_argument("file")
    ap.add_argument("--max-age", type=float, default=900.0, help="seconds; older than this prints nothing")
    a = ap.parse_args(argv)
    text = show(a.file, a.max_age)
    if text is None:
        print(f"{a.file}: missing or older than {a.max_age:g} s (is its poller running?)", file=sys.stderr)
        return 1
    sys.stdout.write(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
