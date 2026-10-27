"""A fake of Jira's REST API, speaking the real shapes, for what the Jira bridge reads. Data Center
first (the owner's: ``/rest/api/2``, a personal access token as ``Bearer``, users by ``name`` and
``key``), and Cloud (``/rest/api/3``, Basic with an email and an API token, users by ``accountId``).

It serves ``/search`` (Data Center: ``startAt``, ``total``) or ``/search/jql`` (Cloud:
``nextPageToken``, ``isLast``) with ``fields`` honoured and ``expand=changelog``;
``/issue/<key>?expand=changelog`` (Data Center) and ``/issue/<key>/changelog`` (Cloud) for a
changelog a search cut short; ``/issue/<key>/comment``; ``/myself``, ``/field`` and ``/status``. It
understands the JQL the bridge writes (``project in (...)``, ``updated >= "-Nm"`` against its own
clock, ``statusCategory = Done`` or ``!= Done``, ``assignee = currentUser()``, ``AND``, parentheses,
``ORDER BY``) and answers anything else 400, as Jira does.

The test drives it as people would act (``create``, ``transition``, ``assign``, ``comment``, ...).
``fail_next`` answers the next requests with a status and headers (a 429 with ``Retry-After``), and
``asleep`` drops every connection, as a site does when the laptop is off the network. Every
summary, description, comment body, sprint goal, link's summary, status and issue type description,
and the before and after of a summary or description change carry ``CANARY``, which must never
leave the bridge. Keys follow the real API (as of 2026-10; ``tests/contract`` compares them)."""

from __future__ import annotations

import base64
import itertools
import json
import re
import threading
import urllib.parse
from collections.abc import Callable
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Self

from fake_github import CANARY

TOKEN = "jira-test-token"
EMAIL = "owner@example.invalid"
SITE = "https://jira.example.invalid"
SPRINT = {"datacenter": "customfield_10100", "cloud": "customfield_10020"}
# id -> (name, category id, category key)
STATUSES = {"1": ("Open", 2, "new"), "3": ("In Progress", 4, "indeterminate"), "10001": ("In Review", 4, "indeterminate"),
            "10002": ("Done", 3, "done")}
CATEGORY_NAMES = {2: ("To Do", "blue-gray"), 4: ("In Progress", "yellow"), 3: ("Done", "green")}
PRIORITIES = {"1": "Highest", "2": "High", "3": "Medium", "4": "Low"}
PROJECTS = {"ABC": "10000", "OPS": "10001", "XYZ": "10002"}


def ts(t: float) -> str:
    """Jira's time format: 2026-10-26T07:33:20.000+0000."""
    return datetime.fromtimestamp(t, UTC).strftime("%Y-%m-%dT%H:%M:%S.000+0000")


class BadJQL(Exception):
    pass


def _split_and(s: str) -> list[str]:
    parts, depth, quote, cur, i = [], 0, False, "", 0
    while i < len(s):
        ch = s[i]
        if ch == '"':
            quote = not quote
        elif not quote and ch == "(":
            depth += 1
        elif not quote and ch == ")":
            depth -= 1
        if not quote and depth == 0 and s[i:i + 5].upper() == " AND ":
            parts.append(cur.strip())
            cur, i = "", i + 5
            continue
        cur += ch
        i += 1
    return [*parts, cur.strip()]


def _wrapped(s: str) -> bool:
    if not (s.startswith("(") and s.endswith(")")):
        return False
    depth = 0
    for i, ch in enumerate(s):
        depth += ch == "("
        depth -= ch == ")"
        if depth == 0 and i < len(s) - 1:
            return False
    return True


class FakeJira:
    def __init__(self, deployment: str = "datacenter", clock: Callable[[], float] | None = None, page_max: int = 50,
                 changelog_max: int | None = None, comment_max: int | None = None):
        self.deployment, self.cloud = deployment, deployment == "cloud"
        self.clock = clock or (lambda: datetime.now(UTC).timestamp())
        self.page_max, self.changelog_max, self.comment_max = page_max, changelog_max, comment_max
        self.sprint_field = SPRINT[deployment]
        self.prefix = "/rest/api/3" if self.cloud else "/rest/api/2"
        self.me = "owner"
        self.apps: set[str] = set()                     # Cloud: logins that are app accounts (bots)
        self.issues: dict[str, dict] = {}
        self.requests: list[tuple[str, dict, str | None]] = []
        self.fail_next: list[tuple[int, dict]] = []
        self.asleep = False
        self._ids = itertools.count(10100)
        self._keys: dict[str, itertools.count] = {}
        self._server: ThreadingHTTPServer | None = None
        self.base = ""

    def user(self, login: str) -> dict:
        avatars = {s: f"{SITE}/secure/useravatar?size={s}" for s in ("48x48", "24x24", "16x16", "32x32")}
        if self.cloud:
            return {"self": f"{SITE}/rest/api/3/user?accountId=acc-{login}", "accountId": f"acc-{login}",
                    "avatarUrls": avatars, "displayName": f"{login.title()} Person", "active": True, "timeZone": "UTC",
                    "accountType": "app" if login in self.apps else "atlassian"}
        key = login if login in ("bob", "owner") else f"JIRAUSER{10000 + sum(map(ord, login))}"
        return {"self": f"{SITE}/rest/api/2/user?username={login}", "name": login, "key": key,
                "emailAddress": f"{login}@example.invalid", "avatarUrls": avatars,
                "displayName": f"{login.title()} Person", "active": True, "timeZone": "UTC"}

    def account(self, login: str) -> str:
        """What a change of assignee names: Cloud's account id, Data Center's user key."""
        u = self.user(login)
        return u["accountId"] if self.cloud else u["key"]

    def login_of(self, login: str) -> str:
        """What the bridge should publish for this person: Cloud's account id, Data Center's username."""
        return self.user(login)["accountId"] if self.cloud else login

    def status(self, sid: str) -> dict:
        name, cid, key = STATUSES[sid]
        cname, colour = CATEGORY_NAMES[cid]
        return {"self": f"{SITE}{self.prefix}/status/{sid}", "description": f"{CANARY} status description",
                "iconUrl": f"{SITE}/images/icons/statuses/generic.png", "name": name, "id": sid,
                "statusCategory": {"self": f"{SITE}{self.prefix}/statuscategory/{cid}", "id": cid, "key": key,
                                   "colorName": colour, "name": cname}}

    def _priority(self, pid: str) -> dict:
        return {"self": f"{SITE}{self.prefix}/priority/{pid}", "iconUrl": f"{SITE}/images/icons/priorities/p.svg",
                "name": PRIORITIES[pid], "id": pid}

    def _sprint(self, sid: int, state: str = "active") -> object:
        if self.cloud:
            return {"id": sid, "name": f"Sprint {sid}", "state": state, "boardId": 3, "goal": f"{CANARY} goal",
                    "startDate": "2026-10-20T09:00:00.000Z", "endDate": "2026-11-03T09:00:00.000Z"}
        return (f"com.atlassian.greenhopper.service.sprint.Sprint@6f2b1c[id={sid},rapidViewId=3,state={state.upper()},"
                f"name=Sprint {sid},goal={CANARY} goal,startDate=2026-10-20T09:00:00.000Z,"
                f"endDate=2026-11-03T09:00:00.000Z,completeDate=<null>,activatedDate=2026-10-20T09:00:00.000Z,sequence={sid}]")

    def _body(self, text: str) -> object:
        if self.cloud:                                  # Atlassian Document Format
            return {"type": "doc", "version": 1,
                    "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]}]}
        return text

    def _history(self, key: str, login: str | None, at: float, items: list[dict]) -> str:
        hid = str(next(self._ids))
        if not self.cloud:
            items = [{k: v for k, v in it.items() if k != "fieldId"} for it in items]
        h = {"id": hid, "created": ts(at), "items": items}
        if login:                                       # Jira's own change has no author at all
            h = {"id": hid, "author": self.user(login), "created": ts(at), "items": items}
        self.issues[key]["histories"].append(h)
        self.issues[key]["updated"] = max(self.issues[key]["updated"], at)
        return hid

    def create(self, project: str, login: str, at: float, *, issuetype: str = "Task", assignee: str | None = None,
               priority: str = "3", labels: tuple[str, ...] = ()) -> str:
        n = next(self._keys.setdefault(project, itertools.count(1)))
        key = f"{project}-{n}"
        self.issues[key] = {"id": str(next(self._ids)), "key": key, "project": project, "issuetype": issuetype,
                            "status": "1", "priority": priority, "labels": list(labels), "sprints": [], "links": [],
                            "creator": login, "reporter": login, "assignee": assignee, "created": at, "updated": at,
                            "duedate": None, "histories": [], "comments": []}
        return key

    def transition(self, key: str, login: str | None, at: float, to: str) -> str:
        i = self.issues[key]
        old, i["status"] = i["status"], to
        return self._history(key, login, at, [
            {"field": "status", "fieldtype": "jira", "fieldId": "status", "from": old, "fromString": STATUSES[old][0],
             "to": to, "toString": STATUSES[to][0]},
            *([{"field": "resolution", "fieldtype": "jira", "fieldId": "resolution", "from": None, "fromString": None,
                "to": "10000", "toString": "Done"}] if STATUSES[to][2] == "done" else [])])

    def assign(self, key: str, login: str, at: float, to: str | None) -> str:
        i = self.issues[key]
        old, i["assignee"] = i["assignee"], to
        return self._history(key, login, at, [
            {"field": "assignee", "fieldtype": "jira", "fieldId": "assignee",
             "from": self.account(old) if old else None, "fromString": f"{old.title()} Person" if old else None,
             "to": self.account(to) if to else None, "toString": f"{to.title()} Person" if to else None}])

    def edit_text(self, key: str, login: str, at: float) -> str:
        """A summary and a description rewritten: the changelog holds the old and new text."""
        return self._history(key, login, at, [
            {"field": "summary", "fieldtype": "jira", "fieldId": "summary", "from": None,
             "fromString": f"{CANARY} old summary", "to": None, "toString": f"{CANARY} new summary"},
            {"field": "description", "fieldtype": "jira", "fieldId": "description", "from": None,
             "fromString": f"{CANARY} old description", "to": None, "toString": f"{CANARY} new description"}])

    def set_priority(self, key: str, login: str, at: float, to: str) -> str:
        i = self.issues[key]
        old, i["priority"] = i["priority"], to
        return self._history(key, login, at, [{"field": "priority", "fieldtype": "jira", "fieldId": "priority",
                                               "from": old, "fromString": PRIORITIES[old], "to": to,
                                               "toString": PRIORITIES[to]}])

    def label(self, key: str, login: str, at: float, add: tuple[str, ...] = (), remove: tuple[str, ...] = ()) -> str:
        i = self.issues[key]
        old = list(i["labels"])
        i["labels"] = [x for x in old if x not in remove] + [x for x in add if x not in old]
        return self._history(key, login, at, [{"field": "labels", "fieldtype": "jira", "fieldId": "labels",
                                               "from": None, "fromString": " ".join(old) or None, "to": None,
                                               "toString": " ".join(i["labels"]) or None}])

    def sprint(self, key: str, login: str, at: float, sid: int) -> str:
        i = self.issues[key]
        old = list(i["sprints"])
        i["sprints"] = [*old, sid]
        return self._history(key, login, at, [
            {"field": "Sprint", "fieldtype": "custom", "fieldId": self.sprint_field,
             "from": ", ".join(map(str, old)) or None, "fromString": ", ".join(f"Sprint {s}" for s in old) or None,
             "to": ", ".join(map(str, i["sprints"])), "toString": ", ".join(f"Sprint {s}" for s in i["sprints"])}])

    def link(self, key: str, login: str, at: float, other: str) -> str:
        self.issues[key]["links"].append(other)
        return self._history(key, login, at, [{"field": "Link", "fieldtype": "jira", "from": None, "fromString": None,
                                               "to": other, "toString": f"This issue blocks {other}"}])

    def due(self, key: str, login: str, at: float, date: str) -> str:
        i = self.issues[key]
        old, i["duedate"] = i["duedate"], date
        return self._history(key, login, at, [{"field": "duedate", "fieldtype": "jira", "fieldId": "duedate",
                                               "from": old, "fromString": f"{old} 00:00:00.0" if old else None,
                                               "to": date, "toString": f"{date} 00:00:00.0"}])

    def comment(self, key: str, login: str, at: float, *, restricted: bool = False) -> str:
        cid = str(next(self._ids))
        c = {"self": f"{SITE}{self.prefix}/issue/{self.issues[key]['id']}/comment/{cid}", "id": cid,
             "author": self.user(login), "body": self._body(f"{CANARY} comment"), "updateAuthor": self.user(login),
             "created": ts(at), "updated": ts(at)}
        if restricted:
            c["visibility"] = {"type": "role", "value": "Developers"}
        if self.cloud:
            c["jsdPublic"] = True
        self.issues[key]["comments"].append(c)
        self.issues[key]["updated"] = max(self.issues[key]["updated"], at)
        return cid

    def _link(self, other: str) -> dict:
        o = self.issues.get(other) or {"id": "1", "status": "1", "priority": "3", "issuetype": "Task"}
        return {"id": str(next(self._ids)), "self": f"{SITE}{self.prefix}/issueLink/1",
                "type": {"id": "10000", "name": "Blocks", "inward": "is blocked by", "outward": "blocks",
                         "self": f"{SITE}{self.prefix}/issueLinkType/10000"},
                "outwardIssue": {"id": o["id"], "key": other, "self": f"{SITE}{self.prefix}/issue/{o['id']}",
                                 "fields": {"summary": f"{CANARY} linked summary", "status": self.status(o["status"]),
                                            "priority": self._priority(o["priority"]),
                                            "issuetype": self._issuetype(o["issuetype"])}}}

    def _issuetype(self, name: str) -> dict:
        return {"self": f"{SITE}{self.prefix}/issuetype/10002", "id": "10002", "description": f"{CANARY} type description",
                "iconUrl": f"{SITE}/images/icons/issuetypes/task.svg", "name": name, "subtask": False, "avatarId": 10318}

    def _comments(self, i: dict, start: int = 0, limit: int | None = None) -> dict:
        cs = i["comments"]
        page = cs[start:start + limit] if limit is not None else cs[start:]
        out = {"comments": page, "maxResults": len(page), "total": len(cs), "startAt": start}
        if self.cloud:
            out["self"] = f"{SITE}{self.prefix}/issue/{i['id']}/comment"
        return out

    def _fields(self, i: dict, wanted: list[str]) -> dict:
        user = self.user
        everything = {
            "summary": f"{CANARY} summary", "description": self._body(f"{CANARY} description"),
            "environment": f"{CANARY} environment",
            "project": {"self": f"{SITE}{self.prefix}/project/{PROJECTS[i['project']]}", "id": PROJECTS[i["project"]],
                        "key": i["project"], "name": f"Project {i['project']}", "projectTypeKey": "software",
                        "avatarUrls": {}},
            "issuetype": self._issuetype(i["issuetype"]), "status": self.status(i["status"]),
            "priority": self._priority(i["priority"]), "labels": list(i["labels"]),
            "issuelinks": [self._link(k) for k in i["links"]],
            "creator": user(i["creator"]), "reporter": user(i["reporter"]),
            "assignee": user(i["assignee"]) if i["assignee"] else None,
            "created": ts(i["created"]), "updated": ts(i["updated"]), "duedate": i["duedate"],
            "comment": self._comments(i, 0, self.comment_max),
            self.sprint_field: [self._sprint(s) for s in i["sprints"]] or None}
        if wanted == ["*all"]:
            return everything
        return {f: everything.get(f) for f in wanted}

    def issue_json(self, i: dict, fields: list[str], changelog: bool) -> dict:
        out = {"expand": "operations,versionedRepresentations,editmeta,changelog,renderedFields", "id": i["id"],
               "self": f"{SITE}{self.prefix}/issue/{i['id']}", "key": i["key"], "fields": self._fields(i, fields)}
        if changelog:
            hs = i["histories"] if self.changelog_max is None else i["histories"][:self.changelog_max]
            out["changelog"] = {"startAt": 0, "maxResults": len(hs), "total": len(i["histories"]), "histories": hs}
        return out

    def matches(self, jql: str, i: dict) -> bool:
        jql = re.split(r"\s+ORDER\s+BY\s+", jql.strip(), flags=re.IGNORECASE)[0].strip()
        return all(self._term(t, i) for t in _split_and(jql))

    def _term(self, t: str, i: dict) -> bool:
        if _wrapped(t):
            return self.matches(t[1:-1], i)
        if m := re.fullmatch(r'project\s+in\s*\(([^)]*)\)', t, re.IGNORECASE):
            return i["project"] in {p.strip().strip('"') for p in m.group(1).split(",")}
        if m := re.fullmatch(r'project\s*=\s*"?([A-Z][A-Z0-9_]*)"?', t, re.IGNORECASE):
            return i["project"] == m.group(1)
        if m := re.fullmatch(r'updated\s*>=\s*"-(\d+)m"', t, re.IGNORECASE):
            return i["updated"] >= self.clock() - int(m.group(1)) * 60
        if m := re.fullmatch(r'statusCategory\s*(!?=)\s*Done', t, re.IGNORECASE):
            return (STATUSES[i["status"]][2] == "done") == (m.group(1) == "=")
        if re.fullmatch(r"assignee\s*=\s*currentUser\(\)", t, re.IGNORECASE):
            return i["assignee"] == self.me
        raise BadJQL(t)

    def search(self, q: dict) -> list[dict]:
        hits = [i for i in self.issues.values() if self.matches(q.get("jql", ""), i)]
        return sorted(hits, key=lambda i: (i["updated"], i["id"]))

    def route(self, rest: list[str], q: dict) -> tuple[int, object]:
        fields = [f for f in q.get("fields", "*all").split(",") if f]
        changelog = "changelog" in q.get("expand", "")
        per = min(int(q.get("maxResults", 50)), self.page_max)
        if rest == ["search"] and not self.cloud or rest == ["search", "jql"] and self.cloud:
            try:
                hits = self.search(q)
            except BadJQL as e:
                return 400, {"errorMessages": [f"Error in the JQL Query: {e}"], "errors": {}}
            start = int(q.get("nextPageToken", "0") if self.cloud else q.get("startAt", 0))
            page = [self.issue_json(i, fields, changelog) for i in hits[start:start + per]]
            if self.cloud:
                more = start + per < len(hits)
                return 200, {"issues": page, **({"nextPageToken": str(start + per)} if more else {}), "isLast": not more}
            return 200, {"expand": "names,schema", "startAt": start, "maxResults": per, "total": len(hits), "issues": page}
        if rest == ["myself"]:
            me = self.user(self.me)
            if not self.cloud:
                me |= {"locale": "en_US", "groups": {"size": 1, "items": []}, "applicationRoles": {"size": 1, "items": []},
                       "expand": "groups,applicationRoles"}
            return 200, me
        if rest == ["status"]:
            return 200, [self.status(s) for s in STATUSES]
        if rest == ["field"]:
            return 200, [
                {"id": "summary", "name": "Summary", "custom": False, "orderable": True, "navigable": True,
                 "searchable": True, "clauseNames": ["summary"], "schema": {"type": "string", "system": "summary"}},
                {"id": self.sprint_field, "name": "Sprint", "custom": True, "orderable": True, "navigable": True,
                 "searchable": True, "clauseNames": [f"cf[{self.sprint_field.split('_')[1]}]", "Sprint"],
                 "schema": {"type": "array", "items": "string" if not self.cloud else "json",
                            "custom": "com.pyxis.greenhopper.jira:gh-sprint",
                            "customId": int(self.sprint_field.split("_")[1])}}]
        if len(rest) >= 2 and rest[0] == "issue" and rest[1] in self.issues:
            i = self.issues[rest[1]]
            if len(rest) == 2 and not self.cloud:
                return 200, self.issue_json({**i}, fields, True) | {
                    "changelog": {"startAt": 0, "maxResults": len(i["histories"]), "total": len(i["histories"]),
                                  "histories": i["histories"]}}
            if rest[2:] == ["changelog"] and self.cloud:
                start, n = int(q.get("startAt", 0)), min(int(q.get("maxResults", 100)), self.page_max)
                vals = i["histories"][start:start + n]
                return 200, {"self": "", "maxResults": n, "startAt": start, "total": len(i["histories"]),
                             "isLast": start + n >= len(i["histories"]), "values": vals}
            if rest[2:] == ["comment"]:
                start, n = int(q.get("startAt", 0)), min(int(q.get("maxResults", 100)), self.page_max)
                return 200, self._comments(i, start, n)
        return 404, {"errorMessages": ["Issue does not exist or you do not have permission to see it."], "errors": {}}

    def authorised(self, auth: str | None) -> bool:
        if self.cloud:
            return auth == "Basic " + base64.b64encode(f"{EMAIL}:{TOKEN}".encode()).decode()
        return auth == f"Bearer {TOKEN}"

    def start(self) -> Self:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # quiet
                pass

            def _send(self, status: int, body, headers: dict | None = None):
                raw = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json;charset=UTF-8")
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                if fake.asleep:                          # off the network: the connection just drops
                    self.close_connection = True
                    return
                u = urllib.parse.urlsplit(self.path)
                q = dict(urllib.parse.parse_qsl(u.query))
                auth = self.headers.get("Authorization")
                fake.requests.append((u.path, q, auth))
                if not fake.authorised(auth):
                    return self._send(401, {"errorMessages": ["You are not authenticated."], "errors": {}})
                if fake.fail_next:
                    status, headers = fake.fail_next.pop(0)
                    return self._send(status, {"errorMessages": ["Rate limit exceeded."], "errors": {}}, headers)
                if not u.path.startswith(fake.prefix + "/"):
                    return self._send(404, {"errorMessages": ["Not found"], "errors": {}})
                status, body = fake.route(u.path[len(fake.prefix) + 1:].split("/"), q)
                return self._send(status, body)

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self._server.server_address[1]}"
        threading.Thread(target=self._server.serve_forever, daemon=True).start()
        return self

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()

    def __enter__(self) -> Self:
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()
