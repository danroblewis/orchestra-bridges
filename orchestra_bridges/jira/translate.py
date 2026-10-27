"""Records as events to publish: types, stable ids, metadata-only data.

The subject is the issue's key (``ABC-12``), the time Jira's, the source whoever acted (the bridge
maps the account, ``identity.py``; a change with no author is Jira's own, published as the bridge).

| Type | When | Data besides the common fields |
|---|---|---|
| ``jira.issue.created`` | the issue's creation, by its creator | ``status``, ``status_id``, ``category`` it started in |
| ``jira.issue.assigned`` | a change of assignee, or an assignee set at creation | ``assignee`` and ``previous`` (accounts), ``assignee_machine`` |
| ``jira.issue.transitioned`` | a change of status | ``from``, ``to`` (status names), ``from_id``, ``to_id``, ``from_category``, ``to_category`` |
| ``jira.issue.updated`` | a change of sprint, priority, labels, links or due date | ``fields`` (names) and ``changes``: ids, label names, keys, dates |
| ``jira.comment.added`` | a comment | ``comment`` (id), ``comments`` (its place among the issue's comments), ``author``, ``restricted`` |

Common fields: ``site``, ``project``, ``issue`` (Jira's id), ``key``, ``issuetype``, ``login`` (the
actor's account), and the issue as the bridge read it: ``status_category`` (its category now),
``priority`` (name) and ``priority_id``, ``labels``, ``sprints`` (ids, not closed) and ``links``
(keys).

**Stable ids**, from Jira's own: ``jira:<site>:issue:<issue id>:created`` (and ``:created:assignee``),
``…:history:<changelog id>:status`` / ``:assignee`` / ``:updated``, ``…:comment:<comment id>``. The
issue's id, not its key, so an issue moved to another project keeps its ids (its subject is the new
key from then on)."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field

from .records import UPDATED, Change, Issue, User, iso, parse_time

TYPES = ("jira.issue.created", "jira.issue.assigned", "jira.issue.transitioned", "jira.issue.updated",
         "jira.comment.added")
RANK = {t: i for i, t in enumerate(TYPES)}       # the order of things Jira stamps with one time


def prefix(site: str) -> str:
    """The start of the id of every event a bridge of ``site`` publishes about an issue."""
    return f"jira:{site}:issue:"


@dataclass(frozen=True)
class Seen:
    id: str
    type: str
    time: str                       # RFC 3339, Jira's
    subject: str
    actor: User | None              # None: Jira itself
    data: dict = field(default_factory=dict, compare=False)

    @property
    def ts(self) -> float:
        return parse_time(self.time)


def _base(site: str, i: Issue, actor: User | None) -> dict:
    return {"site": site, "project": i.project, "issue": i.id, "key": i.key, "issuetype": i.issuetype,
            "login": actor.login if actor else None, "status_category": i.category, "priority": i.priority_name,
            "priority_id": i.priority, "labels": list(i.labels), "sprints": list(i.sprints), "links": list(i.links)}


def _ids(v: str | None) -> list[int]:
    return [int(x) for x in (v or "").replace(" ", "").split(",") if x.isdigit()]


def _change(c: Change) -> dict:
    if c.field == "labels":
        old, new = set((c.old or "").split()), set((c.new or "").split())
        return {"field": "labels", "added": sorted(new - old), "removed": sorted(old - new)}
    if c.field == "sprint":
        return {"field": "sprint", "from": _ids(c.old), "to": _ids(c.new)}
    return {"field": c.field, "from": c.old, "to": c.new}


def events(site: str, i: Issue, statuses: dict[str, tuple[str | None, str | None]],
           machine_of: Callable[[str], str]) -> list[Seen]:
    """Everything the issue's record says happened, whether or not it was published before."""
    out: list[Seen] = []
    pre = f"{prefix(site)}{i.id}:"

    def seen(rest: str, type_: str, at: str, actor: User | None, extra: dict) -> None:
        out.append(Seen(pre + rest, type_, iso(at), i.key, actor, {**_base(site, i, actor), **extra}))

    def assignee(new: str | None, old: str | None) -> dict:
        return {"assignee": new, "previous": old, "assignee_machine": machine_of(new) if new else None}

    changes = [(h, c) for h in i.histories for c in h.changes]
    first_status = next((c.old for _, c in changes if c.field == "status"), i.status)
    assignees = [c for _, c in changes if c.field == "assignee"]
    first_assignee = assignees[0].old if assignees else (i.assignee.login if i.assignee else None)
    if i.created:
        name, category = statuses.get(str(first_status), (None, None))
        seen("created", "jira.issue.created", i.created, i.creator,
             {"status": name, "status_id": first_status, "category": category})
        if first_assignee:
            seen("created:assignee", "jira.issue.assigned", i.created, i.creator, assignee(first_assignee, None))
    for h in i.histories:
        for c in h.changes:
            if c.field == "status":
                (fn, fc), (tn, tc) = statuses.get(str(c.old), (None, None)), statuses.get(str(c.new), (None, None))
                seen(f"history:{h.id}:status", "jira.issue.transitioned", h.created, h.author,
                     {"from": fn, "to": tn, "from_id": c.old, "to_id": c.new, "from_category": fc, "to_category": tc})
            elif c.field == "assignee":
                seen(f"history:{h.id}:assignee", "jira.issue.assigned", h.created, h.author, assignee(c.new, c.old))
        updates = [_change(c) for c in h.changes if c.field in UPDATED]
        if updates:
            seen(f"history:{h.id}:updated", "jira.issue.updated", h.created, h.author,
                 {"fields": sorted({u["field"] for u in updates}), "changes": updates})
    for n, c in enumerate(i.comments, 1):
        seen(f"comment:{c.id}", "jira.comment.added", c.created, c.author,
             {"comment": c.id, "comments": n, "author": c.author.login if c.author else None, "restricted": c.restricted})
    return out
