"""jira_poller: a poller that relays what people did to the Jira issues in scope (built with
``orchestra bridges scaffold jira jira_poller``, then completed; AGENTS.md says what it may do).
Jira Data Center's REST API v2 with a personal access token (``Bearer``).

``python jira_poller.py`` (the poller): each poll searches the issues in scope (``[settings] jql``)
updated since the last poll (less ``overlap``), with their changelog, and publishes, as whoever
acted (``person.jira.<login>``; ``bot.jira`` when no one did), with the issue key as the subject:

- ``jira.issue.created`` once per issue created since the poller began reading (stamp: its created
  time; source: its creator);
- ``jira.issue.transitioned`` once per status change (stamp: the change's id; data: from, to);
- ``jira.issue.updated`` once per other change (stamp: the change's id; data: the fields' names).

Each relayed creation and status change is also counted, by status, for the measurement:
``jira_issues_entered_total{status}`` (created into it, or moved into it) and
``jira_issues_left_total{status}`` (moved out of it). Orchestra counts the events by type and
source only (it never reads ``data``), so these counters are what balances the issues in each
status. They are kept in the poller's ``cursor.json``: a restart neither loses nor repeats a count.

``python jira_poller.py measure`` (for a ``[[collect]]`` entry): prints ``jira_issues_total{status}``
(the issues in scope by status, a gauge), ``jira_issue_age_seconds{status}`` (how long ago the oldest
issue not done in that status was created, a gauge) and the poller's two counters.

It keeps metadata only: keys, logins, status and field names (an administrator's), times. Never a
summary, description, comment, a changed field's value or a person's display name."""

from __future__ import annotations

import math
import sys
import time
from datetime import datetime
from pathlib import Path

from orchestra.config import parse_duration

from orchestra_bridges.kits import runner
from orchestra_bridges.kits.poller import actor_source
from orchestra_bridges.kits.pollstate import counters_at

HERE = Path(__file__).resolve().parent
API = "/rest/api/2"
PAGE = 100
ENTERED, LEFT = "jira_issues_entered_total", "jira_issues_left_total"


def parse_time(jira_time: str) -> datetime:
    return datetime.strptime(jira_time, "%Y-%m-%dT%H:%M:%S.%f%z")


def rfc3339(jira_time: str) -> str:
    return parse_time(jira_time).isoformat()


def keep_changes(body: dict) -> dict:
    """What is kept of a search with changelog: keys, logins, times, status names and the names of
    other changed fields. Never a value of another field (it may be a summary or a comment)."""
    out = []
    for i in body.get("issues") or []:
        f = i.get("fields") or {}
        hs = []
        for h in (i.get("changelog") or {}).get("histories") or []:
            items = h.get("items") or []
            status = next((x for x in items if x.get("field") == "status"), None)
            hs.append({"id": h["id"], "by": (h.get("author") or {}).get("name"), "at": h["created"],
                       "from": status.get("fromString") if status else None,
                       "to": status.get("toString") if status else None,
                       "fields": sorted({str(x.get("field")) for x in items if x.get("field") != "status"})})
        out.append({"key": i["key"], "created": f.get("created"), "creator": (f.get("creator") or {}).get("name"),
                    "status": ((f.get("status") or {}).get("name")), "changes": hs})
    return {"total": body.get("total", 0), "issues": out}


def keep_statuses(body: dict) -> dict:
    return {"total": body.get("total", 0), "issues": [
        {"status": i["fields"]["status"]["name"], "category": i["fields"]["status"]["statusCategory"]["key"],
         "created": i["fields"].get("created")} for i in body.get("issues") or []]}


def search(api, jql: str, keep, ttl_s: float | None = None, **params) -> list[dict]:
    out, start = [], 0
    while True:
        page = api.get(f"{API}/search", {"jql": jql, "startAt": start, "maxResults": PAGE, **params}, keep=keep,
                       ttl_s=ttl_s)
        out += page["issues"]
        start += PAGE
        if start >= page["total"] or not page["issues"]:
            return out


def relay(p, issue: dict, created_since: float) -> None:
    """Publish what happened to one issue, once each, and count its moves between statuses."""
    key, changes = issue["key"], sorted(issue["changes"], key=lambda c: (parse_time(c["at"]), c["id"]))
    first = next((c["from"] for c in changes if c["from"] is not None), issue["status"])
    if issue["created"] and parse_time(issue["created"]).timestamp() >= created_since:
        if p.emit("jira.issue.created", key, {"status": first}, source=actor_source("jira", issue["creator"]),
                  stamp="created", time=rfc3339(issue["created"])):
            p.counter(ENTERED, 1, {"status": first}, help="Jira issues that entered the status (created or moved).")
    for c in changes:
        who, at = actor_source("jira", c["by"]), rfc3339(c["at"])
        if c["to"] is not None:
            if p.emit("jira.issue.transitioned", key, {"from": c["from"], "to": c["to"]}, source=who,
                      stamp=c["id"], time=at):
                p.counter(LEFT, 1, {"status": c["from"]}, help="Jira issues that moved out of the status.")
                p.counter(ENTERED, 1, {"status": c["to"]}, help="Jira issues that entered the status (created or moved).")
        if c["fields"]:
            p.emit("jira.issue.updated", key, {"fields": c["fields"]}, source=who, key=f"{key}/{c['id']}",
                   stamp="updated", time=at)


def make_poll(api, settings):
    scope = settings.extra.get("jql", "project = ABC")
    lookback = parse_duration(settings.extra.get("lookback"), 86400.0)
    overlap = parse_duration(settings.extra.get("overlap"), 600.0)

    def poll(p):
        started = time.time()
        cursor = p.cursor if isinstance(p.cursor, dict) else {}
        since = float(cursor.get("since") or started - lookback)
        began = float(cursor.get("began") or since)        # creations before the poller began reading are not news
        minutes = math.ceil((started - since + overlap) / 60)
        jql = f'({scope}) AND updated >= "-{minutes}m" ORDER BY updated ASC'
        # changes are read fresh (ttl 0): a cached answer would hide the newest
        for issue in search(api, jql, keep_changes, fields="status,created,creator", expand="changelog", ttl_s=0):
            relay(p, issue, began)
        p.cursor = {"since": started, "began": began}

    return poll


def measure(api, settings, series) -> None:
    now = time.time()
    counts: dict[str, int] = {}
    oldest: dict[str, float] = {}
    for i in search(api, settings.extra.get("jql", "project = ABC"), keep_statuses, fields="status,created"):
        counts[i["status"]] = counts.get(i["status"], 0) + 1
        if i["category"] != "done" and i["created"]:
            oldest[i["status"]] = min(oldest.get(i["status"], now), parse_time(i["created"]).timestamp())
    series.load_counters(counters_at(settings.poll_dir))
    known = {dict(k).get("status") for t in series.counters.values() for k in t} - {None}
    for status in sorted(set(counts) | known):         # a status emptied reads 0, not a gap
        series.gauge("jira_issues_total", counts.get(status, 0), {"status": status},
                     help="Jira issues in scope, by status (a level).")
    for status, t in sorted(oldest.items()):
        series.gauge("jira_issue_age_seconds", round(now - t), {"status": status},
                     help="Seconds since the oldest issue not done in the status was created.")


def main(argv: list[str] | None = None) -> int:
    return runner.main(HERE, make_poll, argv, measure=measure)


if __name__ == "__main__":
    sys.exit(main())
