"""{{name}}: a poller that relays what people did to the Jira issues in scope (scaffolded from the
orchestra-bridges kit ``jira``; edit freely, AGENTS.md says what it may do), for either flavour of
Jira (``[settings] flavour``):

- **Data Center**: REST API v2, ``/rest/api/2/search`` (``startAt`` paging), people by user name,
  a personal access token as ``Bearer``;
- **Cloud**: REST API v3, ``/rest/api/3/search/jql`` (``nextPageToken`` paging; Cloud removed
  ``/search``), people by account id, an API token with the account's email as HTTP Basic
  (``user_env`` or ``user_file`` names the email; the token file holds the token as issued).

``python {{name}}.py`` (the poller): each poll searches the issues in scope (``[settings] jql``)
updated since the last poll (less ``overlap``), with their changelog, fresh, and publishes each change
made since the poller began once,
with the issue key as the subject and who acted as the source (``person.jira.<account>``;
``bot.jira`` when no one did):

- ``jira.issue.created`` for an issue created since the poller began (as its creator; stamp
  ``created``), and ``jira.issue.assigned`` with it when it was created assigned (Jira writes no
  changelog for that);
- ``jira.issue.assigned`` for each change of assignee (data: the account it went to);
- ``jira.issue.transitioned`` for each status change (data: from, to);
- each ``jira.issue.*`` also carries the issue as it is **now**, when the poll read it: ``status``
  (its name), ``category`` (its status category's key: ``new``, ``indeterminate``, ``done``),
  ``issuetype`` (its name) and ``labels`` (their names), so a workflow's start can refuse an issue
  already done (``"category": {"not": ["done"]}``) or keep one type (a bug, a PRR ticket);
- ``jira.comment.added`` for each comment (as its author; data: the issue and the author's account:
  never its text).

**The first run** starts at ``[settings] since`` (``now`` by default; or a duration back, ``"1d"``,
or a time: ``start_of``), never at the beginning of the project's history: no change made before it
is relayed, and of a catch-up window it relays only the issues not yet done (status category
``done``), so a finished ticket is never relayed as assigned. Later polls relay every change, a
move to Done included.

The JQL's time is relative (``updated >= "-15m"``), so Jira's time zone for the token's user does not
matter. ``python {{name}}.py measure`` (for a ``[[collect]]`` entry) prints ``jira_issues{status,
category}``, the issues in scope by status.

It keeps metadata only: keys, account ids or user names, status names (an administrator's), times.
Never a summary, description, comment or a person's display name or email."""

from __future__ import annotations

import math
import sys
import time
import urllib.parse
from datetime import datetime
from pathlib import Path

from orchestra.config import parse_duration

from orchestra_bridges.kits import runner
from orchestra_bridges.kits.poller import actor_source, start_of

HERE = Path(__file__).resolve().parent
PAGE = 100


def flavour(settings) -> str:
    """``cloud`` or ``datacenter``: ``[settings] flavour``, or for ``auto`` the configured site's host
    (poller.toml's ``base_url``, never a test's fake)."""
    f = str(settings.extra.get("flavour") or "auto")
    if f in ("cloud", "datacenter"):
        return f
    host = (urllib.parse.urlsplit(settings.base_url).hostname or "").lower()
    return "cloud" if host.endswith(".atlassian.net") else "datacenter"


def api_of(settings) -> str:
    return "/rest/api/3" if flavour(settings) == "cloud" else "/rest/api/2"


def auth_format(settings) -> str:
    return "Basic {basic}" if flavour(settings) == "cloud" else "Bearer {token}"


def parse_time(t: str) -> datetime:
    return datetime.strptime(t, "%Y-%m-%dT%H:%M:%S.%f%z")


def rfc3339(t: str) -> str:
    return parse_time(t).isoformat()


def _id(u) -> str | None:
    """A person's account: Cloud's account id, or Data Center's user name. Never a display name."""
    return (u.get("accountId") or u.get("name")) if isinstance(u, dict) else None


def keep_changes(body: dict) -> dict:
    """What is kept of a search page with changelog: keys, accounts, times, status names. Never a
    summary, a display name or another field's value."""
    out = []
    for i in body.get("issues") or []:
        f = i.get("fields") or {}
        hs = []
        for h in (i.get("changelog") or {}).get("histories") or []:
            items = h.get("items") or []
            status = next((x for x in items if x.get("field") == "status"), None)
            assignee = next((x for x in items if x.get("field") == "assignee"), None)
            hs.append({"id": str(h["id"]), "by": _id(h.get("author")), "at": h["created"],
                       "from": status.get("fromString") if status else None,
                       "to": status.get("toString") if status else None,
                       "assignee_changed": assignee is not None,
                       "assigned": assignee.get("to") if assignee else None})
        comments = [{"id": str(c["id"]), "by": _id(c.get("author")), "at": c["created"]}
                    for c in (f.get("comment") or {}).get("comments") or [] if c.get("created")]
        status = f.get("status") or {}
        out.append({"key": i["key"], "created": f.get("created"), "creator": _id(f.get("creator")),
                    "assignee": _id(f.get("assignee")), "status": status.get("name"),
                    "category": (status.get("statusCategory") or {}).get("key"),
                    "issuetype": (f.get("issuetype") or {}).get("name"),
                    "labels": [x for x in f.get("labels") or [] if isinstance(x, str)], "changes": hs,
                    "comments": comments})
    return page_of(body, out)


def keep_statuses(body: dict) -> dict:
    return page_of(body, [{"status": i["fields"]["status"]["name"],
                           "category": i["fields"]["status"]["statusCategory"]["key"]}
                          for i in body.get("issues") or []])


def page_of(body: dict, issues: list) -> dict:
    """A page's issues and where the next one is: Cloud's ``nextPageToken``, or Data Center's ``total``."""
    return {"issues": issues, "total": body.get("total"), "next": body.get("nextPageToken"),
            "last": body.get("isLast")}


def search(api, settings, jql: str, keep, ttl_s: float | None = None, **params) -> list[dict]:
    out, start, token = [], 0, None
    cloud = flavour(settings) == "cloud"
    while True:
        paging = {"nextPageToken": token} if cloud else {"startAt": start}
        page = api.get(f"{api_of(settings)}/search/jql" if cloud else f"{api_of(settings)}/search",
                       {"jql": jql, "maxResults": PAGE, **paging, **params}, keep=keep, ttl_s=ttl_s)
        out += page["issues"]
        if not page["issues"]:
            return out
        if cloud:
            token = page["next"]
            if page["last"] is not False or not token:
                return out
        else:
            start += PAGE
            if start >= (page["total"] or 0):
                return out


def now_of(issue: dict) -> dict:
    """The issue as the poll read it (not as it was at the change): its status, status category,
    type and labels, carried on every ``jira.issue.*`` event."""
    return {"status": issue["status"], "category": issue.get("category"), "issuetype": issue.get("issuetype"),
            "labels": list(issue.get("labels") or [])}


def relay(p, issue: dict, began: float) -> None:
    key, now = issue["key"], now_of(issue)
    changes = sorted((c for c in issue["changes"] if parse_time(c["at"]).timestamp() >= began),   # none from before
                     key=lambda c: (parse_time(c["at"]), int(c["id"]) if c["id"].isdigit() else 0))
    creator = actor_source("jira", issue["creator"])
    if issue["created"] and parse_time(issue["created"]).timestamp() >= began:
        at = rfc3339(issue["created"])
        p.emit("jira.issue.created", key, now, source=creator, stamp="created", time=at)
        if issue["assignee"] and not any(c["assignee_changed"] for c in changes):
            p.emit("jira.issue.assigned", key, {"assignee": issue["assignee"], **now}, source=creator, stamp="created",
                   time=at)
    for c in changes:
        who, at = actor_source("jira", c["by"]), rfc3339(c["at"])
        if c["assignee_changed"]:
            p.emit("jira.issue.assigned", key, {"assignee": c["assigned"], **now}, source=who, stamp=f"{c['id']}:assignee",
                   time=at)
        if c["to"] is not None:
            p.emit("jira.issue.transitioned", key, {"from": c["from"], "to": c["to"], **now}, source=who, stamp=c["id"],
                   time=at)
    for c in issue.get("comments") or []:
        if parse_time(c["at"]).timestamp() >= began:
            p.emit("jira.comment.added", key, {"issue": key, "author": c["by"]}, source=actor_source("jira", c["by"]),
                   stamp=f"comment:{c['id']}", time=rfc3339(c["at"]))


def make_poll(api, settings):
    scope = settings.extra.get("jql", "project = ABC")
    overlap = parse_duration(settings.extra.get("overlap"), 600.0)

    def poll(p):
        started = time.time()
        cursor = p.cursor if isinstance(p.cursor, dict) else {}
        # No cursor yet: the first run reads from [settings] since, never history; it relays what it
        # keeps of that window, and later polls only what was done after it began (``live``), so an
        # issue the first run passed over (done) is never relayed from before. Whole seconds: the
        # other system's times may have no fraction (a change in the same second counts).
        first = not cursor.get("since")
        since = float(cursor.get("since") or math.floor(start_of(settings.extra.get("since"), started)))
        began = since if first else float(cursor.get("live") or cursor.get("began") or since)
        minutes = math.ceil((started - since + overlap) / 60)
        jql = f'({scope}) AND updated >= "-{minutes}m" ORDER BY updated ASC'
        # changes are read fresh (ttl 0): a cached answer would hide the newest
        for issue in search(api, settings, jql, keep_changes, fields="status,assignee,creator,created,comment,issuetype,labels",
                            expand="changelog", ttl_s=0):
            if first and issue["category"] == "done":
                continue                                # a catch-up relays the work not yet done
            relay(p, issue, began)
        p.cursor = {"since": started, "live": math.floor(started) if first else began}

    return poll


def measure(api, settings, series) -> None:
    counts: dict[tuple, int] = {}
    for i in search(api, settings, settings.extra.get("jql", "project = ABC"), keep_statuses, fields="status"):
        counts[(i["status"], i["category"])] = counts.get((i["status"], i["category"]), 0) + 1
    for (status, category), n in sorted(counts.items()):
        series.gauge("jira_issues", n, {"status": status, "category": category}, help="Jira issues in scope by status.")


def main(argv: list[str] | None = None) -> int:
    return runner.main(HERE, make_poll, argv, measure=measure, auth_format=auth_format)


if __name__ == "__main__":
    sys.exit(main())
