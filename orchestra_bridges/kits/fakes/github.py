"""A fake of GitHub's REST API (``https://api.github.com``) for one repository, in its shapes, for
what a poller reads and what a test changes:

- pull requests: ``GET /repos/<r>/pulls`` (``state``, ``head`` as ``owner:branch``, ``sort``,
  ``direction``, ``per_page``/``page``), ``GET /pulls/<n>``, ``POST /pulls`` (422 when one is
  open for the branch), ``GET``/``POST /pulls/<n>/reviews`` (an author's ``APPROVE`` is refused
  with 422, as GitHub does), ``PUT /pulls/<n>/merge``;
- issues: ``GET /repos/<r>/issues`` (pull requests included, each with ``pull_request``, as GitHub
  lists them; ``state``, ``since`` on the update time, ``sort``, ``direction``, paging), ``GET
  /issues/<n>``, ``GET /issues/<n>/events`` (labeled, unlabeled, closed, reopened, merged, review_requested);
- labels: ``GET``/``POST``/``PUT /issues/<n>/labels`` (add; ``PUT`` replaces), ``DELETE
  /issues/<n>/labels/<name>``, ``POST /repos/<r>/labels`` (create one);
- checks: ``GET /commits/<sha>/check-runs`` and ``GET /commits/<sha>/status`` (the combined
  status);
- ``DELETE /git/refs/heads/<branch>``.

The token goes as ``Bearer``. Every title, body, name and check output carries the canary.
Pull requests and issues share one numbering, as on GitHub. A fixture's time may be relative
(``"-3h"``: three hours before the fake started), so a test's world is always recent. The test's
controls (``open_pr``, ``push``, ``label``, ``request_review``, ``review``, ``check``, ``merge``, ``close``,
``open_issue``, ``reopen``) change the world between polls, each by someone (a login; a ``[bot]``
suffix makes a bot). Paging is by ``page`` only: a page shorter than ``per_page`` is the last
(GitHub's ``Link`` header is not served)."""

from __future__ import annotations

import itertools
import re
import zlib
from datetime import UTC, datetime, timedelta
from typing import Any

from .base import CANARY, FakeServer, one

UNITS = {"s": "seconds", "m": "minutes", "h": "hours", "d": "days"}


def now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def when(t: str | None, at: datetime | None = None) -> str | None:
    """A fixture's time: ISO as it is, or relative (``-3h``) to ``at`` (now)."""
    if not t or not (m := re.fullmatch(r"-(\d+)([smhd])", t)):
        return t
    base = at or datetime.now(UTC)
    return (base - timedelta(**{UNITS[m.group(2)]: int(m.group(1))})).strftime("%Y-%m-%dT%H:%M:%SZ")


def user(login: str | None) -> dict | None:
    if not login:
        return None
    bot = login.endswith("[bot]")
    return {"login": login, "id": zlib.crc32(login.encode()) % 10**8, "type": "Bot" if bot else "User", "name": CANARY}


class FakeGitHub(FakeServer):
    fixture_name = "github.json"

    def __init__(self, fixture=None, token: str | None = None, basic_user: str | None = None):
        super().__init__(fixture, token, basic_user)
        started = datetime.now(UTC)
        d = self.data
        self.repo = d.get("repo", "acme/service")
        for k in ("pulls", "issues"):
            for x in d.setdefault(k, []):
                for f in ("created", "closed", "merged", "updated"):
                    x[f] = when(x.get(f), started)
                x.setdefault("labels", [])
        for k in ("events", "reviews", "runs", "statuses"):
            d.setdefault(k, {})
            for rows in d[k].values():
                for r in rows:
                    r["at"] = when(r.get("at"), started)
        top = max([x["number"] for x in d["pulls"] + d["issues"]], default=0)
        self.numbers = itertools.count(top + 1)
        self.ids = itertools.count(9000)

    # what is stored, found
    def _pull(self, n: int) -> dict | None:
        return next((p for p in self.data["pulls"] if p["number"] == n), None)

    def _item(self, n: int) -> dict | None:
        return self._pull(n) or next((i for i in self.data["issues"] if i["number"] == n), None)

    def _updated(self, x: dict) -> str:
        times = [x.get("created"), x.get("closed"), x.get("merged"), x.get("updated"),
                 *(e["at"] for e in self.data["events"].get(str(x["number"]), [])),
                 *(r["at"] for r in self.data["reviews"].get(str(x["number"]), []))]
        return max(t for t in times if t)

    def _event(self, n: int, event: str, by: str, label: str | None = None) -> None:
        e = {"id": next(self.ids), "event": event, "actor": by, "at": now()}
        if label:
            e["label"] = label
        self.data["events"].setdefault(str(n), []).append(e)

    # in GitHub's shapes
    def _labels(self, x: dict) -> list[dict]:
        return [{"name": name, "color": "ededed", "description": CANARY} for name in x["labels"]]

    @staticmethod
    def _title(x: dict) -> str:
        """The canary, after the title a test gave (a ticket's key in it, say)."""
        return f"{x['title']} {CANARY}" if x.get("title") else CANARY

    def _pr(self, p: dict) -> dict:
        return {"number": p["number"], "title": self._title(p), "body": CANARY, "state": "closed" if p.get("closed") else "open",
                "draft": bool(p.get("draft")), "user": user(p["author"]), "labels": self._labels(p),
                "head": {"ref": p["branch"], "sha": p["head"], "label": f"{self.repo.split('/')[0]}:{p['branch']}"},
                "base": {"ref": p.get("base", "main")},
                "created_at": p["created"], "updated_at": self._updated(p), "closed_at": p.get("closed"),
                "merged_at": p.get("merged"), "html_url": f"https://github.com/{self.repo}/pull/{p['number']}"}

    def _issue(self, x: dict, pull: bool) -> dict:
        out = {"number": x["number"], "title": self._title(x), "body": CANARY, "state": "closed" if x.get("closed") else "open",
               "user": user(x["author"]), "labels": self._labels(x), "comments": 0,
               "repository_url": f"https://api.github.com/repos/{self.repo}",
               "created_at": x["created"], "updated_at": self._updated(x), "closed_at": x.get("closed")}
        if pull:
            out["pull_request"] = {"url": f"https://api.github.com/repos/{self.repo}/pulls/{x['number']}",
                                   "merged_at": x.get("merged")}
        return out

    def _events(self, n: int) -> list[dict]:
        out = []
        for e in self.data["events"].get(str(n), []):
            g = {"id": e["id"], "event": e["event"], "actor": user(e["actor"]), "created_at": e["at"]}
            if e.get("label"):
                g["label"] = {"name": e["label"], "color": "ededed"}
            if e.get("reviewer"):
                g["requested_reviewer"] = user(e["reviewer"])
            out.append(g)
        return out

    def _reviews(self, n: int) -> list[dict]:
        return [{"id": r["id"], "user": user(r["user"]), "body": CANARY, "state": r["state"],
                 "commit_id": r.get("commit"), "submitted_at": r["at"]} for r in self.data["reviews"].get(str(n), [])]

    def _runs(self, sha: str) -> dict:
        runs = [{"id": r.get("id", 0), "name": r["name"], "head_sha": sha, "status": r["status"],
                 "conclusion": r.get("conclusion"), "completed_at": r["at"] if r["status"] == "completed" else None,
                 "app": {"slug": r.get("app", "github-actions"), "name": CANARY}, "output": {"title": CANARY,
                                                                                           "summary": CANARY}}
                for r in self.data["runs"].get(sha, [])]
        return {"total_count": len(runs), "check_runs": runs}

    def _status(self, sha: str) -> dict:
        rows = self.data["statuses"].get(sha, [])
        states = {s["state"] for s in rows}
        combined = ("failure" if states & {"failure", "error"} else "pending" if "pending" in states or not rows
                    else "success")
        return {"state": combined, "sha": sha, "total_count": len(rows), "statuses": [
            {"context": s["context"], "state": s["state"], "description": CANARY, "creator": user(s.get("by")),
             "updated_at": s["at"]} for s in rows]}

    # the test's controls
    def open_pr(self, branch: str, by: str = "alice", head: str | None = None, labels: list[str] | None = None,
                title: str | None = None) -> int:
        """A pull request from ``branch`` by ``by``; ``title`` (served before the canary) is the test's."""
        with self._lock:
            n = next(self.numbers)
            self.data["pulls"].append({"number": n, "branch": branch, "head": head or f"{n:07x}{'0' * 33}",
                                       "author": by, "created": now(), "labels": list(labels or []),
                                       **({"title": title} if title else {})})
            return n

    def push(self, n: int, sha: str) -> None:
        with self._lock:
            p = self._pull(n)
            p["head"], p["updated"] = sha, now()

    def label(self, n: int, name: str, add: bool = True, by: str = "alice") -> None:
        with self._lock:
            x = self._item(n)
            if add and name not in x["labels"]:
                x["labels"].append(name)
            elif not add and name in x["labels"]:
                x["labels"].remove(name)
            else:
                return
            self._event(n, "labeled" if add else "unlabeled", by, name)

    def request_review(self, n: int, reviewer: str, by: str = "alice") -> None:
        with self._lock:
            self._event(n, "review_requested", by)
            self.data["events"][str(n)][-1]["reviewer"] = reviewer

    def review(self, n: int, state: str, by: str, commit: str | None = None) -> None:
        """``state``: APPROVED, CHANGES_REQUESTED or COMMENTED."""
        with self._lock:
            self.data["reviews"].setdefault(str(n), []).append(
                {"id": next(self.ids), "user": by, "state": state, "commit": commit or self._pull(n)["head"], "at": now()})

    def check(self, sha: str, conclusion: str | None = "success", name: str = "ci", status: str = "completed",
              app: str = "github-actions") -> None:
        """A check run on ``sha`` (one per name: a second call updates it)."""
        with self._lock:
            runs = self.data["runs"].setdefault(sha, [])
            r = next((r for r in runs if r["name"] == name), None)
            if r is None:
                r = {"id": next(self.ids), "name": name}
                runs.append(r)
            r.update(status=status, conclusion=conclusion if status == "completed" else None, app=app, at=now())

    def merge(self, n: int, by: str = "alice") -> None:
        with self._lock:
            p = self._pull(n)
            p["merged"] = p["closed"] = now()
            self._event(n, "merged", by)
            self._event(n, "closed", by)

    def close(self, n: int, by: str = "alice") -> None:
        with self._lock:
            self._item(n)["closed"] = now()
            self._event(n, "closed", by)

    def reopen(self, n: int, by: str = "alice") -> None:
        with self._lock:
            x = self._item(n)
            x["closed"], x["updated"] = None, now()
            self._event(n, "reopened", by)

    def open_issue(self, by: str = "alice", labels: list[str] | None = None) -> int:
        with self._lock:
            n = next(self.numbers)
            self.data["issues"].append({"number": n, "author": by, "created": now(), "labels": list(labels or [])})
            return n

    # the API
    def route(self, method: str, path: str, query: dict[str, list[str]], body: Any) -> tuple[int, Any]:
        base = f"/repos/{self.repo}"
        if not path.startswith(base + "/") and path != base:
            return 404, {"message": "Not Found"}
        rest, body = path[len(base):], body if isinstance(body, dict) else {}
        if rest == "/pulls" and method == "GET":
            return 200, self._page(query, [self._pr(p) for p in self._pulls(query)])
        if rest == "/pulls" and method == "POST":
            return self._create_pull(body)
        if rest == "/issues" and method == "GET":
            return 200, self._page(query, self._issues(query))
        if rest == "/labels" and method == "POST":
            return 201, {"name": body.get("name"), "color": body.get("color", "ededed")}
        if m := re.fullmatch(r"/pulls/(\d+)(/reviews|/merge)?", rest):
            return self._on_pull(method, int(m.group(1)), m.group(2), body)
        if m := re.fullmatch(r"/issues/(\d+)(/events|/labels)?(?:/([^/]+))?", rest):
            return self._on_issue(method, int(m.group(1)), m.group(2), m.group(3), body)
        if m := re.fullmatch(r"/commits/([0-9a-fA-F]+)/(check-runs|status)", rest):
            return 200, (self._runs if m.group(2) == "check-runs" else self._status)(m.group(1))
        if re.fullmatch(r"/git/refs/heads/.+", rest) and method == "DELETE":
            return 204, None
        return 404, {"message": "Not Found"}

    @staticmethod
    def _page(query: dict, rows: list) -> list:
        size, page = min(int(one(query, "per_page", "30")), 100), max(int(one(query, "page", "1")), 1)
        return rows[(page - 1) * size:page * size]

    def _pulls(self, query: dict) -> list[dict]:
        state, head = one(query, "state", "open"), one(query, "head")
        rows = [p for p in self.data["pulls"]
                if state == "all" or (state == "closed") == bool(p.get("closed"))]
        if head:
            rows = [p for p in rows if f"{self.repo.split('/')[0]}:{p['branch']}" == head]
        by = "created" if one(query, "sort", "created") == "created" else "updated"
        rows.sort(key=lambda p: p["created"] if by == "created" else self._updated(p),
                  reverse=one(query, "direction", "desc") == "desc")
        return rows

    def _issues(self, query: dict) -> list[dict]:
        state, since = one(query, "state", "open"), one(query, "since")
        rows = [(x, False) for x in self.data["issues"]] + [(p, True) for p in self.data["pulls"]]
        rows = [(x, pull) for x, pull in rows if state == "all" or (state == "closed") == bool(x.get("closed"))]
        if since:
            rows = [(x, pull) for x, pull in rows if self._updated(x) >= since]
        by = one(query, "sort", "created")
        rows.sort(key=lambda r: self._updated(r[0]) if by == "updated" else r[0]["created"],
                  reverse=one(query, "direction", "desc") == "desc")
        return [self._issue(x, pull) for x, pull in rows]

    def _create_pull(self, body: dict) -> tuple[int, Any]:
        branch = body.get("head")
        if not branch:
            return 422, {"message": "Validation Failed"}
        if any(p["branch"] == branch and not p.get("closed") for p in self.data["pulls"]):
            return 422, {"message": "Validation Failed", "errors": [{"message": "A pull request already exists"}]}
        n = next(self.numbers)
        self.data["pulls"].append({"number": n, "branch": branch, "head": f"{n:07x}{'0' * 33}", "author": self.data.get("me", "alice"),
                                   "created": now(), "labels": []})
        return 201, self._pr(self._pull(n))

    def _on_pull(self, method: str, n: int, tail: str | None, body: dict) -> tuple[int, Any]:
        p = self._pull(n)
        if p is None:
            return 404, {"message": "Not Found"}
        if tail is None and method == "GET":
            return 200, self._pr(p)
        if tail == "/reviews" and method == "GET":
            return 200, self._reviews(n)
        if tail == "/reviews" and method == "POST":
            me = self.data.get("me", "alice")
            event = body.get("event")
            if event == "APPROVE" and p["author"] == me:
                return 422, {"message": "Unprocessable Entity", "errors": ["Can not approve your own pull request"]}
            state = {"APPROVE": "APPROVED", "REQUEST_CHANGES": "CHANGES_REQUESTED", "COMMENT": "COMMENTED"}.get(event)
            if state is None:
                return 422, {"message": "Validation Failed"}
            self.data["reviews"].setdefault(str(n), []).append(
                {"id": next(self.ids), "user": me, "state": state, "commit": body.get("commit_id") or p["head"], "at": now()})
            return 200, self._reviews(n)[-1]
        if tail == "/merge" and method == "PUT":
            if p.get("closed"):
                return 405, {"message": "Pull Request is not mergeable"}
            p["merged"] = p["closed"] = now()
            self._event(n, "merged", self.data.get("me", "alice"))
            self._event(n, "closed", self.data.get("me", "alice"))
            return 200, {"merged": True, "sha": p["head"], "message": "Pull Request successfully merged"}
        return 405, {"message": "Method Not Allowed"}

    def _on_issue(self, method: str, n: int, tail: str | None, name: str | None, body: dict) -> tuple[int, Any]:
        x = self._item(n)
        if x is None:
            return 404, {"message": "Not Found"}
        pull = self._pull(n) is not None
        if tail is None and method == "GET":
            return 200, self._issue(x, pull)
        if tail == "/events" and method == "GET" and name is None:
            return 200, self._events(n)
        if tail != "/labels":
            return 405, {"message": "Method Not Allowed"}
        me = self.data.get("me", "alice")
        if name is not None:
            if method != "DELETE" or name not in x["labels"]:
                return 404, {"message": "Label does not exist"}
            x["labels"].remove(name)
            self._event(n, "unlabeled", me, name)
            return 200, self._labels(x)
        if method == "GET":
            return 200, self._labels(x)
        wanted = [str(v) for v in body.get("labels") or []]
        if method == "PUT":
            for old in [v for v in x["labels"] if v not in wanted]:
                x["labels"].remove(old)
                self._event(n, "unlabeled", me, old)
        if method in ("POST", "PUT"):
            for v in wanted:
                if v not in x["labels"]:
                    x["labels"].append(v)
                    self._event(n, "labeled", me, v)
            return 200, self._labels(x)
        return 405, {"message": "Method Not Allowed"}


class FakeGitHubOrg(FakeServer):
    """Several repositories (each a ``FakeGitHub``'s world, one lock) behind one GitHub API, with the
    search API across them, as a poller that follows people rather than one repository reads it:
    ``GET /search/issues`` (``q``: ``is:pr``, ``is:issue``, ``is:open``, ``is:closed``,
    ``author:<login>`` (several: any of them), ``org:<owner>``, ``user:<owner>``, ``repo:<owner>/<name>``,
    ``label:<name>``, ``updated:>=<ISO>`` (or ``>``); ``sort=updated``, ``order``, paging by ``page``
    and ``per_page``; at most 1,000 results, as GitHub), each item in the issues' shape with its
    ``repository_url``. Every other path is the repository's: ``/repos/<owner>/<name>/...``.

    ``FakeGitHubOrg(["acme/api", "acme/web"])`` (empty repositories) or ``{"acme/api": fixture}``;
    ``org.repo("acme/api")`` is that repository's ``FakeGitHub``, whose controls change it."""

    def __init__(self, repos, token: str | None = None, basic_user: str | None = None):
        super().__init__({}, token, basic_user)
        given = repos if isinstance(repos, dict) else dict.fromkeys(repos)
        self.repos: dict[str, FakeGitHub] = {}
        for name, fixture in given.items():
            r = FakeGitHub({"pulls": [], "issues": [], **(fixture or {}), "repo": name})
            r._lock = self._lock                       # one world: the server's lock guards every repository
            self.repos[name] = r

    def repo(self, name: str) -> FakeGitHub:
        return self.repos[name]

    def route(self, method: str, path: str, query: dict[str, list[str]], body: Any) -> tuple[int, Any]:
        if path == "/search/issues" and method == "GET":
            return self._search(query)
        m = re.match(r"^/repos/([^/]+/[^/]+)(?:/|$)", path)
        if m and (r := self.repos.get(m.group(1))) is not None:
            return r.route(method, path, query, body)
        return 404, {"message": "Not Found"}

    def _search(self, query: dict) -> tuple[int, Any]:
        terms = (one(query, "q") or "").split()
        if not terms:
            return 422, {"message": "Validation Failed", "errors": [{"code": "missing", "field": "q"}]}
        authors = [t.split(":", 1)[1] for t in terms if t.startswith("author:")]
        owners = [t.split(":", 1)[1] for t in terms if t.startswith(("org:", "user:"))]
        repos = [t.split(":", 1)[1] for t in terms if t.startswith("repo:")]
        labels = [t.split(":", 1)[1] for t in terms if t.startswith("label:")]
        kinds = {t for t in terms if t in ("is:pr", "is:issue", "type:pr", "type:issue")}
        states = {t.split(":", 1)[1] for t in terms if t in ("is:open", "is:closed", "state:open", "state:closed")}
        after = next((t.split(":", 1)[1] for t in terms if t.startswith("updated:")), None)
        rows = []
        for name, r in self.repos.items():
            if (owners and name.split("/")[0] not in owners) or (repos and name not in repos):
                continue
            items = ([(x, True) for x in r.data["pulls"]] if kinds & {"is:pr", "type:pr"} or not kinds else []) + \
                    ([(x, False) for x in r.data["issues"]] if kinds & {"is:issue", "type:issue"} or not kinds else [])
            for x, pull in items:
                if authors and x["author"] not in authors:
                    continue
                if states and ("closed" if x.get("closed") else "open") not in states:
                    continue
                if labels and not set(labels) <= set(x["labels"]):
                    continue
                updated = r._updated(x)
                if after and not _after(updated, after):
                    continue
                rows.append((updated, r._issue(x, pull)))
        rows.sort(key=lambda u: u[0], reverse=one(query, "order", "desc") == "desc")
        size, page = min(int(one(query, "per_page", "30")), 100), max(int(one(query, "page", "1")), 1)
        found = [i for _, i in rows][:1000]
        return 200, {"total_count": len(rows), "incomplete_results": False, "items": found[(page - 1) * size:page * size]}


def _after(updated: str, cond: str) -> bool:
    """``updated`` against a search's ``updated:>=T`` or ``updated:>T`` (``T`` ISO, in UTC)."""
    strict = not cond.startswith(">=")
    t = cond.removeprefix(">=").removeprefix(">")
    return updated > t if strict else updated >= t
