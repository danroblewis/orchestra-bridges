"""{{name}}: a poller that relays what people did to the Jira issues in scope (scaffolded from the
orchestra-bridges kit ``jira``; edit freely, AGENTS.md says what it may do), for either edition of
Jira (``[settings] edition``; the events are the same for both):

- **Data Center**: REST API v2, ``/rest/api/2/search`` (``startAt`` paging), people by user name,
  a personal access token as ``Bearer``;
- **Cloud**: REST API v3, ``/rest/api/3/search/jql`` (``nextPageToken`` paging; Cloud removed
  ``/search``), people by account id, an API token with the account's email as HTTP Basic
  (``user_env`` or ``user_file`` names the email; the token file holds the token as issued).

``edition`` is ``cloud`` (when not set), ``datacenter``, or ``auto``: ``cloud`` on ``*.atlassian.net``,
``datacenter`` anywhere else. ``python {{name}}.py edition`` (once cleared) reads the site's
``/rest/api/2/serverInfo`` and prints the edition its ``deploymentType`` says (``Cloud``: cloud;
``Server`` or ``DataCenter``: datacenter) and the version: nothing else. Either way it only reads.

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
  already done (``"category": {"not": ["done"]}``) or keep one type (a bug, an Epic, a PRR
  ticket); with ``[settings] sprint_field``, also ``sprint``: the name of its open sprint, else its
  latest (Data Center's legacy ``Sprint@…[…,name=…]`` string or Cloud's object; the sprint's goal
  is never kept);
- ``jira.comment.added`` for each comment (as its author; data: the issue and the author's account:
  never its text).

**The first run** starts at ``[settings] since`` (``now`` by default; or a duration back, ``"1d"``,
or a time: ``start_of``). With ``[settings] history = "window"`` (the default) no change made before
it is relayed, and of a catch-up window it relays only the issues not yet done (status category
``done``), so a finished ticket is never relayed as assigned. With ``history = "full"`` (the owner,
2026-11-04: "search for tickets that were updated within the last 7 days and fill in the history
... I want to see the finished tickets") the first run reads every issue in scope **updated** since
``since``, finished ones included, and relays each one's whole history: its creation, every
assignment and status change and every comment, each at the time it happened. All of it is a
replay (``replayed: true``, the poller's mark), so a workflow that offers work refuses it at its
start and an observing one records it. A changelog Jira cut short (its ``total`` above what the
search returned) is read whole from the issue itself. Later polls relay every change, a move to
Done included.

**The scope** is ``[settings] jql``, narrowed to a team when ``[settings] users`` names them:
``(jql) AND (assignee in (...) OR reporter in (...))`` (``scope_of``), for the poll and ``measure``.

The JQL's time is relative (``updated >= "-15m"``), so Jira's time zone for the token's user does not
matter. ``python {{name}}.py measure`` (for a ``[[collect]]`` entry) prints ``jira_issues{status,
category}``, the issues in scope by status.

It keeps metadata only: keys, account ids or user names, status names (an administrator's), times,
and, with ``[settings] display_names`` (on unless false), the name Jira shows for each person, which
their machine's card is shown by (the owner, 2026-11-04: names like ``person.jira.<id>`` say nothing).
The name stays on this machine: it rides on the person's announcement, never on another event.
Never a summary, description, comment or a person's display name or email."""

from __future__ import annotations

import math
import re
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


EDITIONS = ("cloud", "datacenter", "auto")


def edition(settings) -> str:
    """``cloud`` or ``datacenter``: ``[settings] edition`` (``flavour``, its older name), ``cloud``
    when neither is set, or for ``auto`` the configured site's host (poller.toml's ``base_url``,
    never a test's fake)."""
    e = str(settings.extra.get("edition") or settings.extra.get("flavour") or "cloud").lower()
    if e not in EDITIONS:
        raise ValueError(f"[settings] edition is one of {', '.join(EDITIONS)}, not {e!r}")
    if e != "auto":
        return e
    host = (urllib.parse.urlsplit(settings.base_url).hostname or "").lower()
    return "cloud" if host.endswith(".atlassian.net") else "datacenter"


def api_of(settings) -> str:
    return "/rest/api/3" if edition(settings) == "cloud" else "/rest/api/2"


def auth_format(settings) -> str:
    """Cloud: HTTP Basic of the account's email and an API token; Data Center: a personal access
    token as ``Bearer``. Either is read from its file or variable by the runner, never from here."""
    return "Basic {basic}" if edition(settings) == "cloud" else "Bearer {token}"


def keep_server(body: dict) -> dict:
    """What is kept of ``serverInfo``: the deployment type and the version. Never the title or URL."""
    return {"deploymentType": body.get("deploymentType"), "version": body.get("version")}


def show_edition(api, settings) -> list[str]:
    """The site's edition by its ``serverInfo`` (on both editions at ``/rest/api/2/serverInfo``)."""
    info = api.get("/rest/api/2/serverInfo", {}, keep=keep_server, ttl_s=0)
    kind = str(info.get("deploymentType") or "")
    said = "cloud" if kind.lower() == "cloud" else "datacenter" if kind else "unknown"
    return [f'edition = "{said}"\t# deploymentType {kind or "none"}, version {info.get("version") or "unknown"}']


def parse_time(t: str) -> datetime:
    return datetime.strptime(t, "%Y-%m-%dT%H:%M:%S.%f%z")


def rfc3339(t: str) -> str:
    return parse_time(t).isoformat()


def _id(u) -> str | None:
    """A person's account: Cloud's account id, or Data Center's user name."""
    return (u.get("accountId") or u.get("name")) if isinstance(u, dict) else None


def _named(people: dict, u, keys: dict | None = None) -> None:
    """``u``'s display name, kept by account (``[settings] display_names``): the name Jira shows for
    them, which their machine's card is shown by. Only from a whole user object, by the account the
    events use (``_id``): never from a changelog's ``to``, which on Data Center is the user's internal
    key (``JIRAUSER…`` or an opaque id), not their user name. ``keys``: Data Center's key -> user name,
    as the user objects give them, so a changelog's assignee is said by user name too."""
    if not isinstance(u, dict) or not _id(u):
        return
    if isinstance(u.get("displayName"), str) and u["displayName"].strip():
        people[_id(u)] = u["displayName"].strip()
    if keys is not None and u.get("key") and u.get("name") and u["key"] != u["name"]:
        keys[u["key"]] = u["name"]


SPRINT = re.compile(r"\[(.*)\]\s*$", re.DOTALL)


def _sprints(value) -> list[dict]:
    """The sprints of an issue's Agile sprint field: id, name, state (never the goal), from Data
    Center's legacy strings (``…Sprint@1f[id=12,state=ACTIVE,name=Sprint 42,…]``) or Cloud's objects."""
    out = []
    for v in value if isinstance(value, list) else []:
        if isinstance(v, dict):
            out.append({"id": v.get("id"), "name": v.get("name"), "state": str(v.get("state") or "").lower()})
        elif isinstance(v, str) and (m := SPRINT.search(v)):
            parts = dict(kv.split("=", 1) for kv in re.split(r",(?=\w+=)", m.group(1)) if "=" in kv)
            out.append({"id": parts.get("id"), "name": parts.get("name"), "state": (parts.get("state") or "").lower()})
    return [s for s in out if s.get("name")]


def sprint_of(value) -> str | None:
    """The issue's open sprint's name, else its latest's."""
    sprints = _sprints(value)
    active = [s for s in sprints if s["state"] == "active"]
    return (active or sprints or [{}])[-1].get("name")


def scope_of(settings) -> str:
    """The issues in scope: ``[settings] jql``, and with ``users`` only those assigned to or
    reported by one of them."""
    jql = str(settings.extra.get("jql", "project = ABC") or "").strip()
    users = [str(u) for u in settings.extra.get("users") or [] if str(u).strip()]
    if not users:
        return jql
    names = ", ".join('"' + u.replace('"', "") + '"' for u in users)
    team = f"assignee in ({names}) OR reporter in ({names})"
    return f"({jql}) AND ({team})" if jql else team


def keep_changes_of(sprint_field: str | None):
    """``keep_changes``, with the issue's sprint's name from ``sprint_field``."""
    def keep(body: dict) -> dict:
        page = keep_changes(body)
        if sprint_field:
            by_key = {i["key"]: (i.get("fields") or {}).get(sprint_field) for i in body.get("issues") or []}
            for i in page["issues"]:
                i["sprint"] = sprint_of(by_key.get(i["key"]))
        return page
    return keep


def keep_changes(body: dict) -> dict:
    """What is kept of a search page with changelog: keys, accounts, times, status names, and each
    person's display name by account (``people``). Never a
    summary or another field's value."""
    out = []
    for i in body.get("issues") or []:
        f = i.get("fields") or {}
        hs, people, keys = [], {}, {}
        for u in (f.get("creator"), f.get("assignee"), f.get("reporter")):
            _named(people, u, keys)
        for h in (i.get("changelog") or {}).get("histories") or []:
            _named(people, h.get("author"), keys)
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
        for c in (f.get("comment") or {}).get("comments") or []:
            _named(people, c.get("author"), keys)
        for h in hs:                                    # Data Center's assignee by key: by user name, when known
            if h["assigned"] in keys:
                h["assigned"] = keys[h["assigned"]]
        status = f.get("status") or {}
        out.append({"key": i["key"], "created": f.get("created"), "creator": _id(f.get("creator")),
                    "assignee": _id(f.get("assignee")), "status": status.get("name"),
                    "category": (status.get("statusCategory") or {}).get("key"),
                    "issuetype": (f.get("issuetype") or {}).get("name"),
                    "labels": [x for x in f.get("labels") or [] if isinstance(x, str)], "changes": hs,
                    "comments": comments,
                    "changes_cut": int((i.get("changelog") or {}).get("total") or 0) > len(hs),
                    "people": people})
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
    cloud = edition(settings) == "cloud"
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
    now = {"status": issue["status"], "category": issue.get("category"), "issuetype": issue.get("issuetype"),
           "labels": list(issue.get("labels") or [])}
    if "sprint" in issue:
        now["sprint"] = issue["sprint"]
    return now


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


def whole_changes(api, settings, key: str, sprint_field: str) -> list[dict]:
    """``key``'s whole changelog, read from the issue itself (a search may cut it short)."""
    body = api.get(f"{api_of(settings)}/issue/{key}", {"expand": "changelog", "fields": "status,assignee,creator,created"},
                   keep=lambda b: keep_changes({"issues": [b]}), ttl_s=0)
    issues = body.get("issues") or []
    return issues[0]["changes"] if issues else []


def make_poll(api, settings):
    scope = scope_of(settings)
    sprint_field = str(settings.extra.get("sprint_field") or "")
    fields = "status,assignee,creator,created,comment,issuetype,labels" + (f",{sprint_field}" if sprint_field else "")
    overlap = parse_duration(settings.extra.get("overlap"), 600.0)
    full = str(settings.extra.get("history") or "window").lower() == "full"
    names = settings.extra.get("display_names", True) is not False

    def poll(p):
        started = time.time()
        cursor = p.cursor if isinstance(p.cursor, dict) else {}
        # No cursor yet: the first run reads from [settings] since, never history; it relays what it
        # keeps of that window, and later polls only what was done after it began (``live``), so an
        # issue the first run passed over (done) is never relayed from before. Whole seconds: the
        # other system's times may have no fraction (a change in the same second counts).
        first = not cursor.get("since")
        if names and not cursor.get("named"):
            # once: everyone on the issues in scope, named now (people relayed before the names were
            # kept, or by a copy of the kit from before display_names), not only at their next act
            for issue in search(api, settings, scope, keep_changes_of(sprint_field), fields=fields, expand="changelog"):
                for account, shown in (issue.get("people") or {}).items():
                    source = actor_source("jira", account)
                    p.name_actor(source, shown)
                    if not first:
                        p.announce(source)
        since = float(cursor.get("since") or math.floor(start_of(settings.extra.get("since"), started)))
        began = since if first else float(cursor.get("live") or cursor.get("began") or since)
        minutes = math.ceil((started - since + overlap) / 60)
        jql = f'({scope}) AND updated >= "-{minutes}m" ORDER BY updated ASC'
        # changes are read fresh (ttl 0): a cached answer would hide the newest
        for issue in search(api, settings, jql, keep_changes_of(sprint_field), fields=fields, expand="changelog",
                            ttl_s=0):
            if names:                                   # each person's machine shown by the name Jira shows
                for account, shown in (issue.get("people") or {}).items():
                    p.name_actor(actor_source("jira", account), shown)
            if first and full:                          # the whole history of each issue updated in the window
                if issue.get("changes_cut"):
                    issue["changes"] = whole_changes(api, settings, issue["key"], sprint_field)
                relay(p, issue, 0.0)
                continue
            if first and issue["category"] == "done":
                continue                                # a catch-up relays the work not yet done
            relay(p, issue, began)
        p.cursor = {"since": started, "live": math.floor(started) if first else began, **({"named": True} if names else {})}

    return poll


def measure(api, settings, series) -> None:
    counts: dict[tuple, int] = {}
    for i in search(api, settings, scope_of(settings), keep_statuses, fields="status"):
        counts[(i["status"], i["category"])] = counts.get((i["status"], i["category"]), 0) + 1
    for (status, category), n in sorted(counts.items()):
        series.gauge("jira_issues", n, {"status": status, "category": category}, help="Jira issues in scope by status.")


def main(argv: list[str] | None = None) -> int:
    return runner.main(HERE, make_poll, argv, measure=measure, auth_format=auth_format,
                       commands={"edition": show_edition})


if __name__ == "__main__":
    sys.exit(main())
