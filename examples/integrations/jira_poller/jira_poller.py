"""jira_poller: a poller that relays Jira issues' status changes (scaffolded from the orchestra-bridges
kit ``jira``; edit freely, AGENTS.md says what it may do). Jira Data Center's REST API v2 with a
personal access token (``Bearer``).

``python jira_poller.py`` (the poller): each poll searches the issues in scope (``[settings] jql``)
updated since the last poll (less ``overlap``), with their changelog, and publishes
``jira.issue.transitioned`` once per status change, as whoever made it
(``person.jira.<login>``; ``bot.jira`` when no one did), with the issue key as the subject and the
change's id as the stamp. The JQL's time is relative (``updated >= "-15m"``), so Jira's time zone
for the token's user does not matter.

``python jira_poller.py measure`` (for a ``[[collect]]`` entry): prints ``jira_issues{status,
category}``, the issues in scope by status.

It keeps metadata only: keys, logins, status names (an administrator's), times. Never a summary,
description, comment or a person's display name."""

from __future__ import annotations

import math
import sys
import time
from datetime import datetime
from pathlib import Path

from orchestra.config import parse_duration

from orchestra_bridges.kits import runner
from orchestra_bridges.kits.poller import actor_source

HERE = Path(__file__).resolve().parent
API = "/rest/api/2"
PAGE = 100


def rfc3339(jira_time: str) -> str:
    return datetime.strptime(jira_time, "%Y-%m-%dT%H:%M:%S.%f%z").isoformat()


def keep_changes(body: dict) -> dict:
    """What is kept of a search with changelog: keys and status changes, never text."""
    out = []
    for i in body.get("issues") or []:
        hs = []
        for h in (i.get("changelog") or {}).get("histories") or []:
            for item in h.get("items") or []:
                if item.get("field") == "status":
                    hs.append({"id": h["id"], "by": (h.get("author") or {}).get("name"), "at": h["created"],
                               "from": item.get("fromString"), "to": item.get("toString")})
        out.append({"key": i["key"], "changes": hs})
    return {"total": body.get("total", 0), "issues": out}


def keep_statuses(body: dict) -> dict:
    return {"total": body.get("total", 0), "issues": [
        {"status": i["fields"]["status"]["name"], "category": i["fields"]["status"]["statusCategory"]["key"]}
        for i in body.get("issues") or []]}


def search(api, jql: str, keep, ttl_s: float | None = None, **params) -> list[dict]:
    out, start = [], 0
    while True:
        page = api.get(f"{API}/search", {"jql": jql, "startAt": start, "maxResults": PAGE, **params}, keep=keep,
                       ttl_s=ttl_s)
        out += page["issues"]
        start += PAGE
        if start >= page["total"] or not page["issues"]:
            return out


def make_poll(api, settings):
    scope = settings.extra.get("jql", "project = ABC")
    lookback = parse_duration(settings.extra.get("lookback"), 86400.0)
    overlap = parse_duration(settings.extra.get("overlap"), 600.0)

    def poll(p):
        started = time.time()
        since = p.cursor if isinstance(p.cursor, (int, float)) else started - lookback
        minutes = math.ceil((started - since + overlap) / 60)
        jql = f'({scope}) AND updated >= "-{minutes}m" ORDER BY updated ASC'
        # changes are read fresh (ttl 0): a cached answer would hide the newest
        for issue in search(api, jql, keep_changes, fields="status", expand="changelog", ttl_s=0):
            for c in issue["changes"]:
                p.emit("jira.issue.transitioned", issue["key"], {"from": c["from"], "to": c["to"]},
                       source=actor_source("jira", c["by"]), stamp=c["id"], time=rfc3339(c["at"]))
        p.cursor = started

    return poll


def measure(api, settings, series) -> None:
    counts: dict[tuple, int] = {}
    for i in search(api, settings.extra.get("jql", "project = ABC"), keep_statuses, fields="status"):
        counts[(i["status"], i["category"])] = counts.get((i["status"], i["category"]), 0) + 1
    for (status, category), n in sorted(counts.items()):
        series.gauge("jira_issues", n, {"status": status, "category": category}, help="Jira issues in scope by status.")


def main(argv: list[str] | None = None) -> int:
    return runner.main(HERE, make_poll, argv, measure=measure)


if __name__ == "__main__":
    sys.exit(main())
