"""The bridge's reads of Jira, each returning metadata-only records (``records.py``).

- ``site``: who the token belongs to (``/myself``: the owner), the sprint field's id (``/field``,
  unless configured) and every status's category (``/status``), so a transition's categories are
  known for statuses the issue is no longer in.
- ``updated``: the issues followed that changed in the last N minutes (JQL ``updated >= "-Nm"``,
  relative to Jira's own clock, so neither time zone nor JQL's minute resolution matters), with
  their whole changelog and their comments. When the search holds only part of either, the rest is
  read from the issue.
- ``open_issues``: every issue followed that is not done (for the levels), project, status and
  assignee only.
- ``done_count``: how many issues followed are done, by project, reading keys only.
- ``mine``: the owner's open issues (``assignee = currentUser()``) with their comments, for the
  oldest unanswered comment.

Summaries and descriptions are never asked for (``fields`` names what is read). Comment bodies come
with the comments and are dropped in ``records.issue``."""

from __future__ import annotations

import math
from collections import Counter
from dataclasses import dataclass, field

from .api import Jira, JiraError
from .records import SPRINT_SCHEMA, Issue, User, issue, user

UPDATED_FIELDS = ["project", "issuetype", "status", "priority", "labels", "issuelinks", "creator", "reporter",
                  "assignee", "created", "comment"]
LEVEL_FIELDS = ["project", "status", "assignee"]
MINE_FIELDS = ["project", "status", "assignee", "comment"]


@dataclass
class Site:
    me: User
    sprint_field: str | None
    statuses: dict[str, tuple[str | None, str | None]] = field(default_factory=dict)   # id -> (name, category)


def site(api: Jira, sprint_field: str | None) -> Site:
    me = user(api.get("/myself"), api.cloud)
    if me is None or not me.login:
        raise JiraError("GET /myself: named nobody")
    if sprint_field is None:
        sprint_field = next((f.get("id") for f in api.listing("/field") if isinstance(f, dict)
                             and (f.get("schema") or {}).get("custom") == SPRINT_SCHEMA), None)
    return Site(me, sprint_field, statuses(api))


def statuses(api: Jira) -> dict[str, tuple[str | None, str | None]]:
    """Every status's name and category, by id."""
    return {str(s["id"]): (s.get("name"), (s.get("statusCategory") or {}).get("key"))
            for s in api.listing("/status") if isinstance(s, dict) and s.get("id") is not None}


def minutes(seconds: float) -> int:
    """JQL's relative minutes covering ``seconds`` back, and one more for the clocks' skew."""
    return max(1, math.ceil(seconds / 60.0) + 1)


def _whole(api: Jira, raw: dict) -> tuple[list[dict], list[dict]]:
    """The issue's whole changelog and all its comments, reading more when the search cut them short."""
    cl = raw.get("changelog") or {}
    histories = list(cl.get("histories") or [])
    if int(cl.get("total") or 0) > len(histories):
        histories = api.changelog(raw["key"])
    com = (raw.get("fields") or {}).get("comment") or {}
    comments = list(com.get("comments") or [])
    if int(com.get("total") or 0) > len(comments):
        comments = api.comments(raw["key"])
    return histories, comments


def updated(api: Jira, scope: str, seconds: float, s: Site) -> list[Issue]:
    jql = f'({scope}) AND updated >= "-{minutes(seconds)}m" ORDER BY updated ASC'
    fields = UPDATED_FIELDS + ([s.sprint_field] if s.sprint_field else [])
    out = []
    for raw in api.search(jql, fields, changelog=True):
        histories, comments = _whole(api, raw)
        out.append(issue(raw, api.cloud, s.sprint_field, histories, comments))
    return out


def open_issues(api: Jira, scope: str) -> list[Issue]:
    return [issue(raw, api.cloud) for raw in api.search(f"({scope}) AND statusCategory != Done", LEVEL_FIELDS)]


def done_count(api: Jira, scope: str) -> Counter:
    raws = api.search(f"({scope}) AND statusCategory = Done", ["project"], page=1000)
    return Counter(issue(raw, api.cloud).project for raw in raws)


def mine(api: Jira, scope: str) -> list[Issue]:
    out = []
    for raw in api.search(f"({scope}) AND statusCategory != Done AND assignee = currentUser()", MINE_FIELDS):
        _, comments = _whole(api, {**raw, "changelog": {}})
        out.append(issue(raw, api.cloud, comments=comments))
    return out
