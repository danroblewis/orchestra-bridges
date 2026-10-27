"""Writing the first-response counts (``responses.py``) to the project's metrics store.

Each change is written at the time it happened on GitHub, so after a gap (the laptop slept) the
history is filled in as it was; but never before the last point already written for the repository
(its watermark), because a store reads a series as its last value at or before each instant, and a
point slipped in behind a later one would rewrite what was already read. Polling a minute apart,
a change is so stamped up to a poll late. Between changes the series are written again, unchanged,
every poll, so a quiet repository is told from a bridge that stopped. The watermark of a restarted
bridge is the latest point in the store. A store that cannot be reached loses the points, never an
event."""

from __future__ import annotations

import sys

from orchestra.metrics import MetricsStore

from .responses import COUNTERS, Responses

LEVEL = "github_issues_unanswered"


class Measures:
    def __init__(self, store: MetricsStore, machine: str):
        self.store, self.machine = store, machine
        self.watermark: dict[str, float] = {}
        self.written: dict[str, dict[str, float]] = {}      # repo -> each counter's last written value (COUNTERS keys)

    def labels(self, repo: str) -> dict[str, str]:
        return {"machine": self.machine, "repo": repo}

    def resume(self, repo: str) -> None:
        """The watermark, and each counter's last written value, so a restart continues from them."""
        self.written[repo] = {}
        try:
            last = self.store.latest(LEVEL, self.labels(repo))
            for key, (name, extra) in COUNTERS.items():
                if got := self.store.latest(name, {**self.labels(repo), **extra}):
                    self.written[repo][key] = got[1]
        except Exception as e:  # noqa: BLE001 - measuring never stops the bridge
            print(f"bridges.github: metrics store not read ({type(e).__name__}: {e})", file=sys.stderr)
            last = None
        self.watermark[repo] = last[0] if last else 0.0

    def write(self, repo: str, responses: Responses, t: float) -> None:
        """The counts after a change that happened at ``t`` (or now, re-asserted)."""
        ts = max(t, self.watermark.get(repo, 0.0))
        try:
            self.store.write(responses.points(ts, self.labels(repo)))
        except Exception as e:  # noqa: BLE001 - measuring never stops the bridge
            print(f"bridges.github: metrics not written ({type(e).__name__}: {e})", file=sys.stderr)
            return
        self.watermark[repo] = ts
