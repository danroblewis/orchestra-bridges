"""A fake of Jira's REST API, in its shapes, for what a poller reads, in either flavour:

- **Data Center** (``flavour="datacenter"``, the default): REST API v2, ``/rest/api/2/search``
  (GET or POST, ``jql``, ``startAt``/``maxResults`` paging with ``total``, ``fields``), users by
  ``name`` and ``key``, a personal access token as ``Bearer``;
- **Cloud** (``flavour="cloud"``): REST API v3, ``/rest/api/3/search/jql`` (GET or POST,
  ``nextPageToken`` paging with ``isLast``, no ``total``; the old ``/search`` answers 410 Gone, as
  Cloud does), users by ``accountId`` only, HTTP Basic of the account's email and an API token
  (``basic_user``). An account id here is the fixture's login.

Both serve ``/myself`` (the token's account: ``me``), ``/issue/<key>`` and
``/issue/<key>/transitions`` (GET lists them; POST ``{"transition": {"id"}}`` moves the issue,
which a read-only machine never does, but a test may, to change the world).

It understands a small JQL: clauses joined by ``AND``, each ``project = X``, ``project in (X, Y)``,
``status = "S"``, ``status in (...)``, ``statusCategory = Done`` / ``!= Done``, ``assignee = x`` /
``is EMPTY``, ``updated >= "2026-10-31 08:00"`` (or ``-15m``, against now), and ``ORDER BY updated
ASC|DESC`` (or ``key``). ``expand=changelog`` adds each issue's changes (status changes, and the
edits ``edit`` makes) with their authors; anything else is a 400, as Jira answers a query it cannot
parse. Each issue has its ``creator`` (and ``reporter``) when the fixture names one. Every summary,
display name and edited value carries the canary. ``create``, ``move`` and ``edit`` change the
world between polls."""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any

from .base import CANARY, FakeServer, one

API = "/rest/api/2"                      # Data Center's
CLOUD_API = "/rest/api/3"                # Cloud's
FLAVOURS = ("datacenter", "cloud")
CATEGORY = {"new": ("To Do", 2), "indeterminate": ("In Progress", 4), "done": ("Done", 3)}


class BadJQL(ValueError):
    pass


def jira_time(s: str) -> datetime:
    return datetime.strptime(s, "%Y-%m-%dT%H:%M:%S.%f%z")


def stamp(t: datetime) -> str:
    return t.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%S.000+0000")


def _values(s: str) -> list[str]:
    return [v.strip().strip('"') for v in s.strip().strip("()").split(",") if v.strip()]


def _when(v: str, now: datetime) -> datetime:
    v = v.strip().strip('"')
    if m := re.fullmatch(r"-(\d+)([mhdw])", v):
        n, unit = int(m.group(1)), m.group(2)
        return now - timedelta(**{{"m": "minutes", "h": "hours", "d": "days", "w": "weeks"}[unit]: n})
    for fmt in ("%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(v, fmt).replace(tzinfo=UTC)
        except ValueError:
            continue
    raise BadJQL(f"Date value '{v}' for field 'updated' is invalid")


def _balanced(s: str) -> bool:
    depth = 0
    for ch in s:
        depth += {"(": 1, ")": -1}.get(ch, 0)
        if depth < 0:
            return False
    return depth == 0


def _split_and(body: str) -> list[str]:
    """``body`` split on its top-level ``AND``s (not those inside parentheses or quotes)."""
    parts, depth, quote, cur, i = [], 0, False, "", 0
    while i < len(body):
        ch = body[i]
        if ch == '"':
            quote = not quote
        elif not quote:
            depth += {"(": 1, ")": -1}.get(ch, 0)
        if not quote and depth == 0 and body[i:i + 5].upper() == " AND ":
            parts.append(cur)
            cur, i = "", i + 5
            continue
        cur += ch
        i += 1
    return [*parts, cur]


def _clause(c: str, categories: dict[str, str], now: datetime):
    """The predicate of one JQL clause (a parenthesised conjunction too); raises BadJQL."""
    if c.startswith("(") and c.endswith(")") and _balanced(c[1:-1]):
        preds = [_clause(x.strip(), categories, now) for x in _split_and(c[1:-1]) if x.strip()]
        return lambda i: all(p(i) for p in preds)
    if m := re.fullmatch(r"(?i)project\s*=\s*\"?([A-Z][A-Z0-9_]*)\"?", c):
        project = m.group(1)
        return lambda i: i["project"] == project
    if m := re.fullmatch(r"(?i)project\s+in\s*(\(.*\))", c):
        projects = _values(m.group(1))
        return lambda i: i["project"] in projects
    if m := re.fullmatch(r"(?i)status\s*=\s*(\"[^\"]+\"|\w+)", c):
        status = m.group(1).strip('"').lower()
        return lambda i: i["status"].lower() == status
    if m := re.fullmatch(r"(?i)status\s+in\s*(\(.*\))", c):
        statuses = [x.lower() for x in _values(m.group(1))]
        return lambda i: i["status"].lower() in statuses
    if m := re.fullmatch(r"(?i)statusCategory\s*(=|!=)\s*\"?(Done|To Do|In Progress)\"?", c):
        want = {"done": "done", "to do": "new", "in progress": "indeterminate"}[m.group(2).lower()]
        eq = m.group(1) == "="
        return lambda i: (categories.get(i["status"]) == want) == eq
    if re.fullmatch(r"(?i)assignee\s+is\s+empty", c):
        return lambda i: not i.get("assignee")
    if m := re.fullmatch(r"(?i)assignee\s*=\s*\"?([\w.@-]+)\"?", c):
        who = m.group(1)
        return lambda i: i.get("assignee") == who
    if m := re.fullmatch(r"(?i)updated\s*(>=|>|<=|<)\s*(.+)", c):
        t = _when(m.group(2), now)
        cmp = {">=": lambda a, b: a >= b, ">": lambda a, b: a > b, "<=": lambda a, b: a <= b, "<": lambda a, b: a < b}[m.group(1)]
        # Jira compares to the minute: an issue updated at 08:00:30 matches updated >= "08:00"
        return lambda i: cmp(jira_time(i["updated"]).replace(second=0, microsecond=0), t)
    raise BadJQL(f"Error in the JQL Query: '{c}' is not understood")


def compile_jql(jql: str, categories: dict[str, str], now: datetime):
    """``(predicate(issue), order key, reverse)`` for a JQL the fake understands; raises BadJQL."""
    parts = re.split(r"(?i)\s+order\s+by\s+", jql.strip(), maxsplit=1)
    body, order = parts[0], parts[1] if len(parts) > 1 else ""
    if re.match(r"(?i)order\s+by\s+", body):
        body, order = "", re.sub(r"(?i)^order\s+by\s+", "", body)
    preds = [_clause(c.strip(), categories, now) for c in _split_and(body) if c.strip()]
    field, reverse = "key", False
    if order.strip():
        m = re.fullmatch(r"(?i)\s*(updated|created|key)\s*(asc|desc)?\s*", order)
        if not m:
            raise BadJQL(f"Error in the JQL Query: ORDER BY {order!r}")
        field, reverse = m.group(1).lower(), (m.group(2) or "asc").lower() == "desc"

    def order_key(i):
        if field == "key":
            p, n = i["key"].split("-")
            return (p, int(n))
        return jira_time(i[field])

    return (lambda i: all(p(i) for p in preds)), order_key, reverse


class FakeJira(FakeServer):
    fixture_name = "jira.json"

    def __init__(self, fixture=None, token: str | None = None, basic_user: str | None = None, *,
                 flavour: str = "datacenter", me: str = "api-user"):
        if flavour not in FLAVOURS:
            raise ValueError(f"flavour is one of {FLAVOURS}, not {flavour!r}")
        super().__init__(fixture, token, basic_user)
        self.flavour, self.me = flavour, me
        self.api = CLOUD_API if flavour == "cloud" else API
        self.auth_format = "Basic {basic}" if flavour == "cloud" else "Bearer {token}"

    def _user(self, login: str | None) -> dict | None:
        """A person as this flavour names one: Cloud by account id only, Data Center by name and key."""
        if not login:
            return None
        if self.flavour == "cloud":
            return {"accountId": login, "displayName": f"{CANARY} {login}", "emailAddress": CANARY}
        return {"name": login, "key": login, "displayName": f"{CANARY} {login}"}

    def _issue(self, i: dict, fields: list[str] | None = None, changelog: bool = False) -> dict:
        cat = self.data["statuses"].get(i["status"], "new")
        cname, cid = CATEGORY[cat]
        f = {
            "summary": i["summary"], "project": {"key": i["project"], "name": i["project"]},
            "status": {"name": i["status"], "statusCategory": {"id": cid, "key": cat, "name": cname}},
            "issuetype": {"name": i["issuetype"]}, "priority": {"name": i["priority"]},
            "assignee": self._user(i.get("assignee")),
            "created": i["created"], "updated": i["updated"],
            "creator": self._user(i.get("creator")), "reporter": self._user(i.get("creator")),
        }
        if fields and "*all" not in fields:
            f = {k: v for k, v in f.items() if k in fields}
        out = {"id": i["id"], "key": i["key"], "self": f"{self.url}{self.api}/issue/{i['id']}", "fields": f}
        if changelog:
            hs = i.get("history") or []
            out["changelog"] = {"startAt": 0, "maxResults": len(hs), "total": len(hs), "histories": [
                {"id": h["id"], "author": self._user(h["by"]),
                 "created": h["at"], "items": [_item(h)]} for h in hs]}
        return out

    def _record(self, i: dict, to: str, by: str | None, at: datetime) -> None:
        hist = i.setdefault("history", [])
        hid = str(20000 + sum(len(x.get("history") or []) for x in self.data["issues"]) + 1)
        hist.append({"id": hid, "by": by or "automation", "at": stamp(at), "from": i["status"], "to": to})
        i["status"], i["updated"] = to, stamp(at)

    def _find(self, key: str) -> dict | None:
        return next((i for i in self.data["issues"] if i["key"] == key or i["id"] == key), None)

    # the test's controls
    def create(self, key: str, status: str = "Open", by: str | None = "alice", at: datetime | None = None,
               assignee: str | None = None) -> None:
        """A new issue ``key`` (its project is the key's prefix) in ``status``, created by ``by``
        (assigned to ``assignee`` from the start: Jira writes no changelog for that)."""
        with self._lock:
            t = stamp(at or datetime.now(UTC))
            n = 10000 + len(self.data["issues"]) + 1
            self.data["issues"].append({"id": str(n), "key": key, "project": key.split("-")[0],
                                        "summary": f"{CANARY}: {key}", "status": status, "issuetype": "Task",
                                        "priority": "Medium", "assignee": assignee, "creator": by,
                                        "created": t, "updated": t})

    def edit(self, key: str, field: str = "priority", by: str | None = "alice", at: datetime | None = None) -> None:
        """``key``'s ``field`` changed by ``by`` (not its status), recorded in its changelog."""
        with self._lock:
            i, t = self._find(key), at or datetime.now(UTC)
            hid = str(20000 + sum(len(x.get("history") or []) for x in self.data["issues"]) + 1)
            i.setdefault("history", []).append({"id": hid, "by": by or "automation", "at": stamp(t), "field": field,
                                                "from": f"{CANARY} before", "to": f"{CANARY} after"})
            i["updated"] = stamp(t)

    def assign(self, key: str, to: str | None, by: str | None = "alice", at: datetime | None = None) -> None:
        """``key`` assigned to ``to`` (None: unassigned) by ``by``, recorded in its changelog."""
        with self._lock:
            i, t = self._find(key), at or datetime.now(UTC)
            hid = str(20000 + sum(len(x.get("history") or []) for x in self.data["issues"]) + 1)
            i.setdefault("history", []).append({"id": hid, "by": by or "automation", "at": stamp(t), "field": "assignee",
                                                "from": i.get("assignee"), "to": to})
            i["assignee"], i["updated"] = to, stamp(t)

    def move(self, key: str, status: str, by: str | None = "alice", at: datetime | None = None) -> None:
        """``key`` moves to ``status`` by ``by``, recorded in its changelog."""
        with self._lock:
            self._record(self._find(key), status, by, at or datetime.now(UTC))

    def route(self, method: str, path: str, query: dict[str, list[str]], body: Any) -> tuple[int, Any]:
        api = self.api
        q = query if method == "GET" else {**query, **_as_query(body)}
        if path == f"{api}/myself":
            return 200, self._user(self.me)
        if self.flavour == "cloud":
            if path == f"{api}/search/jql":
                return self._search(q, tokens=True)
            if path == f"{api}/search":
                gone = "The requested API has been removed. Please migrate to the /rest/api/3/search/jql API."
                return 410, {"errorMessages": [gone], "errors": {}}
        elif path == f"{api}/search":
            return self._search(q)
        if m := re.fullmatch(rf"{api}/issue/([^/]+)/transitions", path):
            return self._transitions(method, m.group(1), body)
        if m := re.fullmatch(rf"{api}/issue/([^/]+)", path):
            i = self._find(m.group(1))
            if method != "GET":
                return 405, None
            if i is None:
                return 404, {"errorMessages": ["Issue Does Not Exist"], "errors": {}}
            return 200, self._issue(i, _fields(query), "changelog" in (one(query, "expand") or ""))
        return 404, {"errorMessages": ["null for uri"], "errors": {}}

    def _search(self, q: dict[str, list[str]], tokens: bool = False) -> tuple[int, Any]:
        """A page of the issues the JQL matches: ``startAt`` and ``total`` (Data Center), or
        ``nextPageToken`` and ``isLast`` (Cloud, ``tokens``)."""
        try:
            pred, key, rev = compile_jql(one(q, "jql", "") or "", self.data["statuses"], datetime.now(UTC))
            start = int(one(q, "nextPageToken", "0") or "0") if tokens else int(one(q, "startAt", "0"))
            size = min(int(one(q, "maxResults", "50")), 1000)
        except BadJQL as e:
            return 400, {"errorMessages": [str(e)], "errors": {}}
        except ValueError:
            return 400, {"errorMessages": ["startAt, nextPageToken and maxResults are numbers here"], "errors": {}}
        rows = sorted((i for i in self.data["issues"] if pred(i)), key=key, reverse=rev)
        fields = _fields(q)
        page = [self._issue(i, fields, "changelog" in (one(q, "expand") or "")) for i in rows[start:start + size]]
        if tokens:
            last = start + size >= len(rows)
            return 200, {"issues": page, "isLast": last, **({} if last else {"nextPageToken": str(start + size)})}
        return 200, {"expand": "schema,names", "startAt": start, "maxResults": size, "total": len(rows), "issues": page}

    def _transitions(self, method: str, key: str, body: Any) -> tuple[int, Any]:
        i = self._find(key)
        if i is None:
            return 404, {"errorMessages": ["Issue Does Not Exist"], "errors": {}}
        avail = self.data["transitions"].get(i["status"], [])
        if method == "GET":
            return 200, {"expand": "transitions", "transitions": [
                {"id": t["id"], "name": t["name"], "to": {"name": t["to"], "statusCategory": {
                    "key": self.data["statuses"].get(t["to"], "new")}}} for t in avail]}
        tid = str(((body or {}).get("transition") or {}).get("id", ""))
        t = next((t for t in avail if t["id"] == tid), None)
        if t is None:
            return 400, {"errorMessages": [f"Transition id '{tid}' is not valid for this issue."], "errors": {}}
        self._record(i, t["to"], "api-user", datetime.now(UTC))
        return 204, None


def _item(h: dict) -> dict:
    """A changelog item: an assignee's change names the accounts (``from``, ``to``) and shows their
    names (the canary); any other field shows its values only."""
    if h.get("field") == "assignee":
        return {"field": "assignee", "fieldtype": "jira", "from": h["from"], "to": h["to"],
                "fromString": CANARY if h["from"] else None, "toString": CANARY if h["to"] else None}
    return {"field": h.get("field", "status"), "fieldtype": "jira", "fromString": h["from"], "toString": h["to"]}


def _fields(q: dict[str, list[str]]) -> list[str] | None:
    raw = ",".join(q.get("fields") or [])
    return [f.strip() for f in raw.split(",") if f.strip()] or None


def _as_query(body: Any) -> dict[str, list[str]]:
    """A POSTed search's JSON body as query parameters."""
    if not isinstance(body, dict):
        return {}
    out = {}
    for k, v in body.items():
        out[k] = [",".join(v)] if isinstance(v, list) else [str(v)]
    return out
