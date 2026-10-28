"""The first-response counts (``responses.py``) and the gap counts (``gaps.py``), served on
``/metrics`` for the instrument the plugin declares (the scrape ``bridges.github``, ``plugin.py``),
which stores them labelled ``instrument="bridges.github"``. The bridge never writes a metric itself.

Each change is served at the time it happened on GitHub (the sample's own timestamp, which the
instrument keeps), so after a gap (the laptop slept) the history is filled in as it was; but never
before the last point already served for the repository (its watermark), because a store reads a
series as its last value at or before each instant, and a point slipped in behind a later one would
rewrite what was already read. Between changes the series are served again, unchanged, every poll,
so a quiet repository is told from a bridge that stopped.

**Points wait in memory until a scrape takes them.** Each ``GET /metrics`` answers with every point
waiting, several of one series at their own times, and they are then gone; at most ``WAITING``
wait (the oldest go first). A scrape that fails once it has read them loses them, as a store that
could not be reached did; the next carries whole totals, so only the shape between is lost.

On start the watermark, and each counter's last value, are what the instrument stored last for the
repository (read by ``instrument`` and ``repo``), so a restart continues from them. Points the
bridge wrote itself before 2026-10-27 are labelled ``machine="bridges.github"`` instead (``LEGACY``):
read too, never written, and the higher value taken, so the upgrade loses no count."""

from __future__ import annotations

import sys
import threading
from collections import deque

from orchestra.metrics import MetricsStore, Point

from .responses import COUNTERS, Responses

INSTRUMENT = "bridges.github"
LEGACY = {"machine": "bridges.github"}       # how the bridge labelled the points it wrote before 2026-10-27
LEVEL = "github_issues_unanswered"
WAITING = 50_000


def _escape(v: str) -> str:
    return v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _num(v: float) -> str:
    return str(int(v)) if v == int(v) else repr(v)


class Measures:
    def __init__(self, store: MetricsStore, instrument: str = INSTRUMENT):
        self.store, self.instrument = store, instrument
        self.watermark: dict[str, float] = {}
        self.written: dict[str, dict[str, float]] = {}      # repo -> each counter's last stored value (COUNTERS keys)
        self.waiting: deque[Point] = deque(maxlen=WAITING)
        self.ready = False                                   # resumed: /metrics answers
        self.lock = threading.Lock()

    def labels(self, repo: str) -> dict[str, str]:
        """The labels a point is served with (the instrument adds ``instrument`` and ``project``)."""
        return {"repo": repo}

    def stored(self, repo: str) -> dict[str, str]:
        """The labels that find the repository's points in the store."""
        return {"instrument": self.instrument, "repo": repo}

    def _latest(self, name: str, labels: dict[str, str]) -> tuple[float, float] | None:
        """The latest stored point of a series of this instrument, or of the bridge's own writes
        before it (``LEGACY``): the later one's time, and the higher value."""
        got = [p for by in ({"instrument": self.instrument}, LEGACY) if (p := self.store.latest(name, {**by, **labels}))]
        return (max(p[0] for p in got), max(p[1] for p in got)) if got else None

    def resume(self, repo: str) -> None:
        """The watermark, and each counter's last stored value, so a restart continues from them."""
        self.written[repo] = {}
        try:
            last = self._latest(LEVEL, self.labels(repo))
            for key, (name, extra) in COUNTERS.items():
                if got := self._latest(name, {**self.labels(repo), **extra}):
                    self.written[repo][key] = got[1]
        except Exception as e:  # noqa: BLE001 - measuring never stops the bridge
            print(f"{self.instrument}: metrics store not read ({type(e).__name__}: {e})", file=sys.stderr)
            last = None
        self.watermark[repo] = last[0] if last else 0.0
        self.ready = True

    def write(self, repo: str, responses: Responses, t: float, also: dict[str, float] | None = None) -> None:
        """Serve the counts after a change that happened at ``t`` (or now, re-asserted), and
        ``also`` (the gap counts) beside them."""
        ts = max(t, self.watermark.get(repo, 0.0))
        labels = self.labels(repo)
        pts = responses.points(ts, labels) + [Point(n, v, ts, labels) for n, v in (also or {}).items()]
        with self.lock:
            self.waiting.extend(pts)
        self.watermark[repo] = ts

    def render(self) -> str | None:
        """``/metrics``: every point waiting, with its time, which a scrape then has taken; None
        (503) until the bridge has read its past back."""
        if not self.ready:
            return None
        with self.lock:
            pts, self.waiting = list(self.waiting), deque(maxlen=WAITING)
        by_name: dict[str, list[Point]] = {}
        for p in pts:
            by_name.setdefault(p.series, []).append(p)
        lines: list[str] = []
        for name, group in by_name.items():
            lines.append(f"# TYPE {name} {'counter' if name.endswith('_total') else 'gauge'}")
            for p in group:
                inner = ",".join(f'{k}="{_escape(v)}"' for k, v in sorted(p.labels.items()))
                lines.append(f"{name}{{{inner}}} {_num(p.value)} {round(p.ts * 1000)}")
        return "\n".join(lines) + "\n" if lines else ""
