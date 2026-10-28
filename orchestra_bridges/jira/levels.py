"""What the issues followed hold now, as levels for ``/metrics``, read from Jira each round (not
rebuilt from events):

- ``jira_issues{jira_project, category}``: issues by status category (``new``, ``indeterminate``,
  ``done``). Done issues are counted every ``done_every`` (they only pile up, and counting them
  reads every key), the others every round;
- ``jira_issues_mine_not_started{jira_project}``: assigned to the owner (the token's account) and still
  in a ``new`` status;
- ``jira_comments_unanswered{jira_project}``: the owner's open issues where someone else has commented
  since the owner last did;
- ``jira_comment_unanswered_oldest_seconds{jira_project}``: how long the oldest of those comments has
  waited (0 when none waits);
- ``jira_bridge_last_poll_timestamp_seconds``: when the last good poll began (for resume);
- the gap counts (``gaps.py``), added by the bridge: ``jira_bridge_gaps_total`` and the rest.

The Jira project is the label ``jira_project``: ``project`` is Orchestra's own (the scrape keeps a
bridge's ``project`` only as ``exported_project``).

A bot's comment (an app account on Cloud, or one the identity map calls a bot) does not wait for an
answer unless ``bots_ask``. Only a comment by the owner answers: a transition does not. Flows
(created, transitioned, commented) are the bridge's events, which Orchestra counts itself."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable

from .records import Issue, parse_time

CATEGORIES = ("new", "indeterminate", "done")
LAST_POLL = "jira_bridge_last_poll_timestamp_seconds"


def waiting_since(i: Issue, me: str, is_bot: Callable[[str | None, bool], bool], bots_ask: bool) -> float | None:
    """When the oldest comment on ``i`` that the owner has not answered was made, or None."""
    since = None
    for c in i.comments:
        login = c.author.login if c.author else None
        if login == me:
            since = None
        elif since is None and (bots_ask or not is_bot(login, bool(c.author and c.author.bot))):
            since = parse_time(c.created)
    return since


def measure(projects: list[str], open_: list[Issue], done: Counter, mine: list[Issue], me: str,
            is_bot: Callable[[str | None, bool], bool], bots_ask: bool, now: float) -> dict:
    """The levels, keyed by series and then by labels (a tuple of (name, value) pairs)."""
    seen = sorted({*projects, *(i.project for i in open_), *done, *(i.project for i in mine)})
    issues = Counter((i.project, i.category) for i in open_ if i.category in CATEGORIES[:2])
    not_started = Counter(i.project for i in open_ if i.category == "new" and i.assignee and i.assignee.login == me)
    waits: dict[str, list[float]] = {p: [] for p in seen}
    for i in mine:
        if (t := waiting_since(i, me, is_bot, bots_ask)) is not None:
            waits[i.project].append(now - t)
    out: dict[str, dict] = {"jira_issues": {}, "jira_issues_mine_not_started": {}, "jira_comments_unanswered": {},
                            "jira_comment_unanswered_oldest_seconds": {}}
    for p in seen:
        for c in CATEGORIES:
            out["jira_issues"][(("jira_project", p), ("category", c))] = done[p] if c == "done" else issues[(p, c)]
        out["jira_issues_mine_not_started"][(("jira_project", p),)] = not_started[p]
        out["jira_comments_unanswered"][(("jira_project", p),)] = len(waits[p])
        out["jira_comment_unanswered_oldest_seconds"][(("jira_project", p),)] = round(max(waits[p], default=0.0), 1)
    return out


def _escape(v: str) -> str:
    return v.replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")


def _num(v: float) -> str:
    return str(int(v)) if v == int(v) else repr(v)


def render(levels: dict, last_poll: float) -> str:
    """The Prometheus text format."""
    lines: list[str] = []
    for name, by_labels in levels.items():
        lines.append(f"# TYPE {name} {'counter' if name.endswith('_total') else 'gauge'}")
        for labels, value in sorted(by_labels.items()):
            inner = ",".join(f'{k}="{_escape(v)}"' for k, v in labels)
            lines.append(f"{name}{{{inner}}} {_num(value)}" if inner else f"{name} {_num(value)}")
    lines += [f"# TYPE {LAST_POLL} gauge", f"{LAST_POLL} {_num(last_poll)}"]
    return "\n".join(lines) + "\n"
