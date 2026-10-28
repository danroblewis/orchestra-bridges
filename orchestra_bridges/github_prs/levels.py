"""What waits on whom among the open pull requests the bridge follows, as levels for ``/metrics``.

Each round, every open followed pull request is read as GitHub shows it now (the pulls list, its
timeline, its head's checks), so the levels are GitHub's state, not something rebuilt from events:

- ``github_prs_open``: open pull requests followed;
- ``github_prs_awaiting_review``: open, not drafts, with a review asked of someone (a person or a
  team) who has not given it;
- ``github_prs_awaiting_changes``: a reviewer's latest verdict is "changes requested", on the commit
  that is still the head: the author has not pushed since;
- ``github_prs_approved``: approved by someone, with nobody's latest verdict "changes requested";
- ``github_prs_awaiting_label_result``: carrying a label of ``label_results`` (e.g. ``SAIL``) and
  none of the labels that report its result;
- ``github_prs_checks_failing``: the head's latest run of some check (by name) or status (by
  context) failed;
- ``github_prs_review_request_oldest_seconds``: how long the oldest review still asked for has been
  asked (from GitHub's record of the request, else from when the pull request was opened).

A reviewer's verdict is their latest review that approves, requests changes or was dismissed; a
review that only comments gives none. Flows (opened, merged, reviews submitted) are the bridge's
events, which Orchestra counts on its own; nothing here counts them twice. Beside them the bridge
serves its gap counts (``gaps.py``) and ``github_prs_bridge_last_poll_timestamp_seconds``."""

from __future__ import annotations

from dataclasses import dataclass, field

from .translate import Pull, parse_time

FAILED = ("failure", "timed_out", "error")
LAST_POLL = "github_prs_bridge_last_poll_timestamp_seconds"
SERIES = ("github_prs_open", "github_prs_awaiting_review", "github_prs_awaiting_changes", "github_prs_approved",
          "github_prs_awaiting_label_result", "github_prs_checks_failing", "github_prs_review_request_oldest_seconds")


@dataclass
class PullState:
    pull: Pull
    verdicts: dict[str, tuple[str, str | None]] = field(default_factory=dict)   # reviewer -> (state, commit)
    asked: dict[str, float] = field(default_factory=dict)                       # reviewer or team:<slug> -> when last asked
    checks: dict[str, tuple[float, str]] = field(default_factory=dict)          # check or status:<context> -> (t, conclusion)

    @classmethod
    def read(cls, p: Pull, timeline: list[dict], runs: list[dict], statuses: list[dict]) -> PullState:
        s = cls(p)
        reviews = sorted((e for e in timeline if e.get("event") == "reviewed" and e.get("submitted_at")),
                         key=lambda e: e["submitted_at"])
        for r in reviews:
            login, state = (r.get("user") or {}).get("login"), (r.get("state") or "").lower()
            if login and state in ("approved", "changes_requested"):
                s.verdicts[login] = (state, r.get("commit_id"))
            elif login and state == "dismissed":
                s.verdicts.pop(login, None)
        for e in timeline:
            if e.get("event") == "review_requested" and e.get("created_at"):
                who = (e.get("requested_reviewer") or {}).get("login") or f"team:{(e.get('requested_team') or {}).get('slug')}"
                s.asked[who] = max(s.asked.get(who, 0.0), parse_time(e["created_at"]))
        for r in runs:
            if r.get("status") == "completed" and r.get("completed_at") and r.get("head_sha", p.head) == p.head:
                t = parse_time(r["completed_at"])
                if t >= s.checks.get(r.get("name") or "", (0.0, ""))[0]:
                    s.checks[r.get("name") or ""] = (t, r.get("conclusion") or "")
        for st in statuses:
            key = f"status:{st.get('context')}"
            if st.get("created_at") and st.get("state") != "pending":
                t = parse_time(st["created_at"])
                if t >= s.checks.get(key, (0.0, ""))[0]:
                    s.checks[key] = (t, st.get("state") or "")
        return s

    def awaiting_review(self) -> bool:
        return not self.pull.draft and bool(self.pull.reviewers or self.pull.teams)

    def awaiting_changes(self) -> bool:
        return any(state == "changes_requested" and commit == self.pull.head for state, commit in self.verdicts.values())

    def approved(self) -> bool:
        states = [state for state, _ in self.verdicts.values()]
        return "approved" in states and "changes_requested" not in states

    def awaiting_label_result(self, results: dict[str, list[str]]) -> bool:
        have = set(self.pull.labels)
        return any(label in have and not have & set(done) for label, done in results.items())

    def checks_failing(self) -> bool:
        return any(conclusion in FAILED for _, conclusion in self.checks.values())

    def oldest_request(self, now: float) -> float:
        opened = parse_time(self.pull.created_at)
        pending = [*self.pull.reviewers, *(f"team:{t}" for t in self.pull.teams)]
        return max((now - self.asked.get(who, opened) for who in pending), default=0.0)


def levels(states: list[PullState], results: dict[str, list[str]], now: float) -> dict[str, float]:
    return {"github_prs_open": len(states),
            "github_prs_awaiting_review": sum(s.awaiting_review() for s in states),
            "github_prs_awaiting_changes": sum(s.awaiting_changes() for s in states),
            "github_prs_approved": sum(s.approved() for s in states),
            "github_prs_awaiting_label_result": sum(s.awaiting_label_result(results) for s in states),
            "github_prs_checks_failing": sum(s.checks_failing() for s in states),
            "github_prs_review_request_oldest_seconds": round(max((s.oldest_request(now) for s in states), default=0.0), 1)}


def _escape(v: str) -> str:
    return v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def render(by_repo: dict[str, tuple[dict[str, float], float]]) -> str:
    """The Prometheus text format: each repository's levels and when its last good poll began."""
    lines: list[str] = []
    extra = sorted({n for values, _ in by_repo.values() for n in values} - set(SERIES))
    for name in (*SERIES, *extra, LAST_POLL):
        lines.append(f"# TYPE {name} {'counter' if name.endswith('_total') else 'gauge'}")
        for repo, (values, last_poll) in sorted(by_repo.items()):
            value = last_poll if name == LAST_POLL else values.get(name, 0.0)
            lines.append(f'{name}{{repo="{_escape(repo)}"}} {int(value) if value == int(value) else value!r}')
    return "\n".join(lines) + "\n"
