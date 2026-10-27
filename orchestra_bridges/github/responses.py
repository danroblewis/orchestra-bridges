"""First response to issues: which opened issues still wait for anyone but their author, and how
long the answered ones waited.

An issue the bridge saw opened waits until someone other than its author comments on it or closes
it (labels do not answer: triage bots label everything). A bot's comment or close counts only with
``bots_answer``. Closed by its author, or by a bot that does not count, before anyone answered, it
stops waiting unanswered. Issues opened before the bridge first watched are not counted; a reopened
issue does not wait again.

The counts, as series (``measures.py``), all labelled with the repository:

- ``github_issues_unanswered``: the issues waiting now (a level);
- ``github_issues_unanswered_oldest_seconds``: how long the oldest has waited;
- ``github_issues_opened_total``: issues opened while watched;
- ``github_issues_answered_total{within}``: answered, by how long it took (``15m``, ``1h``, ``1d``,
  ``over``), since a counter keeps no histogram;
- ``github_issue_first_response_seconds_total``: the waits of the answered ones added up (the mean
  is its rate over the answered rate);
- ``github_issues_closed_unanswered_total``: closed before anyone answered.

So ``unanswered`` changes by opened − answered − closed unanswered, exactly.

The state is rebuilt from the bridge's own events on the bus when it starts (folded in time order),
so it is kept nowhere else."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field

from orchestra.metrics import Point

WITHIN = ((15 * 60, "15m"), (3600, "1h"), (86400, "1d"))


def within(seconds: float) -> str:
    return next((name for limit, name in WITHIN if seconds <= limit), "over")


@dataclass
class Responses:
    bots_answer: bool = False
    waiting: dict[str, tuple[str, float]] = field(default_factory=dict)   # subject -> (author's source, opened at)
    opened: int = 0
    answered: Counter = field(default_factory=Counter)                       # within -> count
    closed_unanswered: int = 0
    response_seconds: float = 0.0

    def fold(self, type_: str, subject: str, source: str, t: float) -> bool:
        """Take one event (time order); whether any count changed."""
        if type_ == "issue.opened":
            if subject in self.waiting:
                return False
            self.waiting[subject] = (source, t)
            self.opened += 1
            return True
        if subject not in self.waiting or type_ not in ("issue.commented", "issue.closed"):
            return False
        author, since = self.waiting[subject]
        answers = source != author and (self.bots_answer or not source.startswith("bot."))
        if answers:
            del self.waiting[subject]
            wait = max(t - since, 0.0)
            self.answered[within(wait)] += 1
            self.response_seconds += wait
            return True
        if type_ == "issue.closed":
            del self.waiting[subject]
            self.closed_unanswered += 1
            return True
        return False

    def oldest(self, now: float) -> float:
        return max((now - t for _, t in self.waiting.values()), default=0.0)

    def points(self, ts: float, labels: dict[str, str]) -> list[Point]:
        """Every series at ``ts``: the levels and every counter, so they balance at each instant."""
        pts = [Point("github_issues_unanswered", len(self.waiting), ts, labels),
               Point("github_issues_unanswered_oldest_seconds", round(self.oldest(ts), 1), ts, labels),
               Point("github_issues_opened_total", self.opened, ts, labels),
               Point("github_issues_closed_unanswered_total", self.closed_unanswered, ts, labels),
               Point("github_issue_first_response_seconds_total", round(self.response_seconds, 1), ts, labels)]
        pts += [Point("github_issues_answered_total", self.answered[name], ts, {**labels, "within": name})
                for name in [n for _, n in WITHIN] + ["over"]]
        return pts
