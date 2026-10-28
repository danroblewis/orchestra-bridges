"""Gaps in what a bridge saw, as measurements of its instrument, never as events.

A gap (the laptop slept, the bridge was stopped, the other system or the network was down) is no
act of any machine, so it is not on the bus. A poll that succeeds more than ``ROUNDS`` rounds after
the last good one closes a gap, and the bridge counts it, per scope (a repository, or a Jira site),
in three counters it serves on ``/metrics`` for the instrument the plugin declares:

- ``<prefix>_bridge_gaps_total``: gaps closed;
- ``<prefix>_bridge_gaps_recovered_total``: of those, the ones read back whole (no longer than
  ``max_catchup``); the rest lost what lay beyond it;
- ``<prefix>_bridge_gap_seconds_total``: seconds from the last good poll to the one that closed
  each gap.

On start each counter continues from the value its instrument stored last (read by the
``instrument`` label), so a restart never makes one go down; a store that cannot be read starts
them at zero, which a reader of counters takes as a reset."""

from __future__ import annotations

from collections.abc import Callable

from orchestra.metrics import MetricsStore

ROUNDS = 3          # a poll this many rounds late closes a gap


class Gaps:
    def __init__(self, prefix: str):
        self.names = (f"{prefix}_bridge_gaps_total", f"{prefix}_bridge_gaps_recovered_total",
                      f"{prefix}_bridge_gap_seconds_total")
        self.counts: dict[str, list[float]] = {}           # scope -> the three counters, in ``names`` order

    def resume(self, scope: str, store: MetricsStore, labels: dict[str, str],
               log: Callable[[str], None] | None = None) -> None:
        """Continue the scope's counters from what its instrument stored (``labels`` find them)."""
        got = [0.0, 0.0, 0.0]
        try:
            for n, name in enumerate(self.names):
                if last := store.latest(name, labels):
                    got[n] = last[1]
        except Exception as e:  # noqa: BLE001 - the store never stops the bridge
            if log:
                log(f"gap counts not read back ({type(e).__name__}); they start again from zero")
        self.counts[scope] = got

    def close(self, scope: str, seconds: float, recovered: bool) -> None:
        c = self.counts.setdefault(scope, [0.0, 0.0, 0.0])
        c[0] += 1
        c[1] += 1 if recovered else 0
        c[2] = round(c[2] + seconds, 1)

    def values(self, scope: str) -> dict[str, float]:
        return dict(zip(self.names, self.counts.get(scope, [0.0, 0.0, 0.0]), strict=True))

    @staticmethod
    def late(last_ok: float | None, now: float, every: float) -> bool:
        """Whether a poll at ``now`` closes a gap since the last good one."""
        return bool(last_ok) and now - last_ok > ROUNDS * every
