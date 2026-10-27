"""The pings waiting for the owner, and how long the answered ones waited, as Prometheus text.

Served at ``/metrics`` (``serve.py``) for a ``[[scrape]]``, every series labelled with the
workspace:

- ``slack_pings_unanswered``: the pings waiting now (the stock);
- ``slack_pings_oldest_unanswered_seconds``: how long the oldest has waited;
- ``slack_pings_received_total``: pings received while watched (the inflow);
- ``slack_pings_answered_total{within="5m|15m|60m|over"}``: answered, by how long it took, since a
  scrape keeps no histogram buckets;
- ``slack_pings_expired_total``: stopped waiting unanswered (``pings.py``);
- ``slack_ping_wait_seconds_total``: the answered waits added up (the mean is its rate over the
  answered rate);
- ``slack_bridge_last_poll_timestamp_seconds``: when the last successful poll began (Unix seconds),
  absent before the first. A scrape stores it even while polls fail, so on restart its value, not
  the time of the scrape, says where the bridge left off.

So ``unanswered`` changes by received − answered − expired, exactly. The state is rebuilt from the
bridge's own events when it starts, and every counter then continues from the larger of what was
rebuilt and what the store last holds for it (``continue_from``): a trimmed bus never takes a
counter down, and a count the store had not scraped yet is not lost."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from .pings import ANSWERED, EXPIRED, RECEIVED

WITHIN = ((300, "5m"), (900, "15m"), (3600, "60m"))
BUCKETS = [*(n for _, n in WITHIN), "over"]
LEVEL = "slack_pings_unanswered"
OLDEST = "slack_pings_oldest_unanswered_seconds"
LAST_POLL = "slack_bridge_last_poll_timestamp_seconds"
COUNTERS = {"received": ("slack_pings_received_total", {}),
            "expired": ("slack_pings_expired_total", {}),
            "wait_seconds": ("slack_ping_wait_seconds_total", {}),
            **{f"answered:{n}": ("slack_pings_answered_total", {"within": n}) for n in BUCKETS}}


def within(seconds: float) -> str:
    return next((name for limit, name in WITHIN if seconds <= limit), "over")


@dataclass
class Waits:
    waiting: dict[tuple[str, str], tuple[str, float]] = field(default_factory=dict)   # (channel, ts) -> (subject, t)
    received: int = 0
    answered: Counter = field(default_factory=Counter)                                 # within -> count
    expired: int = 0
    wait_seconds: float = 0.0
    offset: dict[str, float] = field(default_factory=dict)

    def fold(self, type_: str, channel: str, ts: str, subject: str, t: float) -> float | None:
        """Take one event (time order). For an answer, the wait; else 0 when a count changed, None
        when nothing did."""
        key = (channel, ts)
        if type_ == RECEIVED:
            if key in self.waiting:
                return None
            self.waiting[key] = (subject, t)
            self.received += 1
            return 0.0
        if key not in self.waiting or type_ not in (ANSWERED, EXPIRED):
            return None
        _, since = self.waiting.pop(key)
        if type_ == EXPIRED:
            self.expired += 1
            return 0.0
        wait = max(t - since, 0.0)
        self.answered[within(wait)] += 1
        self.wait_seconds += wait
        return wait

    def in_thread(self, subject: str, before: float) -> list[tuple[str, str]]:
        """The pings waiting in thread ``subject`` that came before ``before``, oldest first."""
        return sorted((k for k, (s, t) in self.waiting.items() if s == subject and t < before), key=lambda k: float(k[1]))

    def oldest(self, now: float) -> float:
        return max((now - t for _, t in self.waiting.values()), default=0.0)

    def counts(self) -> dict[str, float]:
        """Each counter (``COUNTERS``) as folded, before any offset."""
        return {"received": self.received, "expired": self.expired, "wait_seconds": self.wait_seconds,
                **{f"answered:{n}": self.answered[n] for n in BUCKETS}}

    def continue_from(self, written: dict[str, float]) -> None:
        """Continue each counter from the larger of its folded value and its last written one."""
        self.offset = {k: max(written[k] - v, 0.0) for k, v in self.counts().items() if k in written}

    def values(self, now: float) -> list[tuple[str, dict[str, str], float]]:
        """Every series: (name, labels beside the workspace's, value)."""
        counts = self.counts()
        out = [(LEVEL, {}, float(len(self.waiting))), (OLDEST, {}, round(self.oldest(now), 1))]
        out += [(name, extra, round(counts[k] + self.offset.get(k, 0.0), 1)) for k, (name, extra) in COUNTERS.items()]
        return out

    def exposition(self, now: float, labels: dict[str, str], last_poll: float | None = None) -> str:
        """The Prometheus text format, one sample per series; ``last_poll`` too, when there was one."""
        lines, typed = [], set()
        last = [(LAST_POLL, {}, last_poll)] if last_poll else []
        for name, extra, value in [*self.values(now), *last]:
            if name not in typed:
                lines.append(f"# TYPE {name} {'counter' if name.endswith('_total') else 'gauge'}")
                typed.add(name)
            ls = ",".join(f'{k}="{_escape(v)}"' for k, v in {**labels, **extra}.items())
            lines.append(f"{name}{{{ls}}} {int(value) if value == int(value) else value!r}")
        return "\n".join(lines) + "\n"


def _escape(v: str) -> str:
    return v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
