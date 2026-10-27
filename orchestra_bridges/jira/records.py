"""Jira's JSON as metadata-only records, made at the point of reading so that text never leaves here.

A search returns an issue's summary, description, comment bodies and every change's before and after
(``fromString``, ``toString``: a description's old and new text). These functions copy ids, keys,
account ids, names an administrator defines (issue type, priority, status), label names, sprint ids
and times; nothing a person wrote to someone. The rest is dropped with the JSON.

Accounts: on Data Center a user is their username (``name``), with their user ``key`` kept so the
identity map may list either; a change of assignee names the user key, which is turned back into the
username wherever the issue's JSON shows both. On Cloud a user is their ``accountId`` (an app's
``accountType`` is ``app``: a bot, such as Automation for Jira). Jira's display name is kept for
the person's announcement (the owner chose people by name), never for an event.

Of an issue's changes, only those the bridge publishes are kept: status, assignee, and the fields of
``jira.issue.updated`` (sprint, priority, labels, links, due date)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime

SPRINT_SCHEMA = "com.pyxis.greenhopper.jira:gh-sprint"
UPDATED = ("sprint", "priority", "labels", "link", "duedate")      # the fields jira.issue.updated reports
_DC_SPRINT = re.compile(r"\bid=(\d+).*?\bstate=([A-Za-z]+)")


def parse_time(s: str) -> float:
    """Jira's ``2026-10-26T07:33:20.000+0000`` (or RFC 3339) as Unix seconds."""
    return datetime.fromisoformat(s).timestamp()


def iso(s: str) -> str:
    """Jira's time as RFC 3339, as events carry it."""
    return datetime.fromisoformat(s).isoformat()


@dataclass(frozen=True)
class User:
    login: str | None              # Data Center: username (name); Cloud: accountId
    bot: bool = False
    name: str | None = None        # Jira's display name, for announcements only
    key: str | None = None         # Data Center's user key (JIRAUSER10100, or the username on older sites)


def user(u: object, cloud: bool) -> User | None:
    if not isinstance(u, dict):
        return None
    if cloud:
        return User(u.get("accountId"), u.get("accountType") == "app", u.get("displayName"))
    return User(u.get("name") or u.get("key"), False, u.get("displayName"), u.get("key"))


@dataclass(frozen=True)
class Change:
    field: str                     # status, assignee, sprint, priority, labels, link, duedate
    old: str | None                # an id, an account, label names or an issue key: never a person's words
    new: str | None


@dataclass(frozen=True)
class History:
    id: str
    author: User | None            # None: Jira itself (a workflow, an import)
    created: str
    changes: tuple[Change, ...]


@dataclass(frozen=True)
class Comment:
    id: str
    author: User | None
    created: str
    restricted: bool               # visible to a role or group only


@dataclass(frozen=True)
class Issue:
    id: str
    key: str
    project: str
    issuetype: str | None
    status: str | None             # the status's id
    category: str | None           # its category: new, indeterminate or done
    priority: str | None           # the priority's id
    priority_name: str | None
    labels: tuple[str, ...]
    sprints: tuple[int, ...]       # sprints it is in that are not closed
    links: tuple[str, ...]         # keys of the issues it is linked to
    creator: User | None
    created: str
    assignee: User | None
    histories: tuple[History, ...] = ()
    comments: tuple[Comment, ...] = ()


def _sprints(value: object) -> tuple[int, ...]:
    out: list[int] = []
    for s in value if isinstance(value, list) else []:
        if isinstance(s, dict) and isinstance(s.get("id"), int) and s.get("state") != "closed":
            out.append(s["id"])
        elif isinstance(s, str) and (m := _DC_SPRINT.search(s)) and m.group(2).lower() != "closed":
            out.append(int(m.group(1)))          # Data Center: "...Sprint@1a2b[id=12,rapidViewId=3,state=ACTIVE,...]"
    return tuple(out)


def _links(value: object) -> tuple[str, ...]:
    keys = []
    for link in value if isinstance(value, list) else []:
        other = (link.get("outwardIssue") or link.get("inwardIssue") or {}) if isinstance(link, dict) else {}
        if other.get("key"):
            keys.append(other["key"])
    return tuple(keys)


def _change(item: dict, sprint_field: str | None, names: dict[str, str]) -> Change | None:
    """One changed field, if the bridge publishes it; only ids, accounts, label names or keys."""
    field, fid = str(item.get("field") or "").lower(), item.get("fieldId")
    if (sprint_field and fid == sprint_field) or field == "sprint":
        return Change("sprint", item.get("from"), item.get("to"))          # "12, 13": sprint ids
    if field == "labels":
        return Change("labels", item.get("fromString"), item.get("toString"))   # label names, space-separated
    if field == "assignee":
        old, new = item.get("from"), item.get("to")                         # Data Center may give user keys
        return Change("assignee", names.get(old, old) if old else None, names.get(new, new) if new else None)
    if field in ("status", "priority", "link", "duedate"):
        return Change(field, item.get("from"), item.get("to"))               # ids, or a linked issue's key
    return None


def _user_names(raw: dict, cloud: bool) -> dict[str, str]:
    """Data Center: user key to username, from every user the issue's JSON shows."""
    if cloud:
        return {}
    f = raw.get("fields") or {}
    users = [f.get("creator"), f.get("reporter"), f.get("assignee")]
    users += [h.get("author") for h in (raw.get("changelog") or {}).get("histories") or []]
    users += [c.get("author") for c in ((f.get("comment") or {}).get("comments") or [])]
    return {u["key"]: u["name"] for u in users if isinstance(u, dict) and u.get("key") and u.get("name")}


def issue(raw: dict, cloud: bool, sprint_field: str | None = None, histories: list[dict] | None = None,
          comments: list[dict] | None = None) -> Issue:
    """A search result (with whichever fields were asked for) as a record."""
    f = raw.get("fields") or {}
    names = _user_names(raw, cloud)
    status = f.get("status") or {}
    priority = f.get("priority") or {}
    hs = []
    for h in histories or []:
        changes = tuple(c for item in h.get("items") or [] if (c := _change(item, sprint_field, names)))
        if changes and h.get("id") is not None and h.get("created"):
            hs.append(History(str(h["id"]), user(h.get("author"), cloud), h["created"], changes))
    cs = [Comment(str(c["id"]), user(c.get("author"), cloud), c["created"], bool(c.get("visibility")))
          for c in comments or [] if c.get("id") is not None and c.get("created")]
    return Issue(
        id=str(raw.get("id")), key=str(raw.get("key")), project=(f.get("project") or {}).get("key") or str(raw.get("key")).split("-")[0],
        issuetype=(f.get("issuetype") or {}).get("name"), status=status.get("id"),
        category=(status.get("statusCategory") or {}).get("key"), priority=priority.get("id"),
        priority_name=priority.get("name"), labels=tuple(f.get("labels") or []),
        sprints=_sprints(f.get(sprint_field)) if sprint_field else (), links=_links(f.get("issuelinks")),
        creator=user(f.get("creator") or f.get("reporter"), cloud), created=f.get("created") or "",
        assignee=user(f.get("assignee"), cloud),
        histories=tuple(sorted(hs, key=lambda h: (parse_time(h.created), h.id))),
        comments=tuple(sorted(cs, key=lambda c: (parse_time(c.created), c.id))))
