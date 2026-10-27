"""A fake of GitHub's REST API, speaking the real shapes, for the three lists the bridge reads.

It serves ``/repos/<o>/<r>/issues``, ``/issues/comments`` and ``/issues/events`` (and the same under
``/repositories/<id>/``, where GitHub's own pagination links point), paged with a Link header,
filtered and ordered as GitHub does (``since``, ``sort=updated``, ``direction``; events newest
first). It demands the bearer token, sends the rate-limit headers, and can be told to answer the
next requests with a secondary rate limit (403 and Retry-After) or a 429. Every object has the keys
GitHub's has (as of 2026-10; ``tests/contract`` compares them with the real API's), and every
title, body and comment carries ``CANARY``, which must never leave the bridge."""

from __future__ import annotations

import itertools
import json
import threading
import urllib.parse
import zlib
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Self

CANARY = "CANARY-7d1f-never-publish-me"
TOKEN = "test-token"
API = "https://api.github.com"


def ts(t: float) -> str:
    return datetime.fromtimestamp(t, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def user(login: str, bot: bool = False) -> dict:
    uid = zlib.crc32(login.encode()) % 10_000_000
    u = f"{API}/users/{login}"
    return {"login": login, "id": uid, "node_id": f"U_{uid}", "avatar_url": f"https://avatars.githubusercontent.com/u/{uid}?v=4",
            "gravatar_id": "", "url": u, "html_url": f"https://github.com/{login}", "followers_url": f"{u}/followers",
            "following_url": f"{u}/following{{/other_user}}", "gists_url": f"{u}/gists{{/gist_id}}",
            "starred_url": f"{u}/starred{{/owner}}{{/repo}}", "subscriptions_url": f"{u}/subscriptions",
            "organizations_url": f"{u}/orgs", "repos_url": f"{u}/repos", "events_url": f"{u}/events{{/privacy}}",
            "received_events_url": f"{u}/received_events", "type": "Bot" if bot else "User", "user_view_type": "public",
            "site_admin": False}


def reactions(url: str) -> dict:
    return {"url": f"{url}/reactions", "total_count": 0, "+1": 0, "-1": 0, "laugh": 0, "hooray": 0, "confused": 0,
            "heart": 0, "rocket": 0, "eyes": 0}


class FakeGitHub:
    def __init__(self, repo: str = "microsoft/vscode", repo_id: int = 41881900, per_page_max: int = 100):
        self.repo, self.repo_id, self.per_page_max = repo, repo_id, per_page_max
        self.issues: dict[int, dict] = {}
        self.comments: list[dict] = []
        self.events: list[dict] = []
        self.requests: list[tuple[str, dict, str | None]] = []
        self.fail_next: list[tuple[int, dict]] = []        # (status, headers) for the next requests
        self.remaining = 5000
        self._ids = itertools.count(1000)
        self._server: ThreadingHTTPServer | None = None
        self.base = ""

    def _issue_url(self, n: int) -> str:
        return f"{API}/repos/{self.repo}/issues/{n}"

    def open(self, n: int, login: str, at: float, *, bot: bool = False, pull: bool = False) -> dict:
        url = self._issue_url(n)
        issue = {"url": url, "repository_url": f"{API}/repos/{self.repo}", "labels_url": f"{url}/labels{{/name}}",
                 "comments_url": f"{url}/comments", "events_url": f"{url}/events",
                 "html_url": f"https://github.com/{self.repo}/{'pull' if pull else 'issues'}/{n}", "id": next(self._ids),
                 "node_id": f"I_{n}", "number": n, "title": f"{CANARY} title", "user": user(login, bot), "labels": [],
                 "state": "open", "locked": False, "assignees": [], "milestone": None, "comments": 0,
                 "created_at": ts(at), "updated_at": ts(at), "closed_at": None, "assignee": None,
                 "author_association": "NONE", "issue_field_values": [], "type": None, "active_lock_reason": None,
                 "sub_issues_summary": {"total": 0, "completed": 0, "percent_completed": 0},
                 "issue_dependencies_summary": {"blocked_by": 0, "total_blocked_by": 0, "blocking": 0, "total_blocking": 0},
                 "body": f"{CANARY} body", "closed_by": None, "reactions": reactions(url), "timeline_url": f"{url}/timeline",
                 "performed_via_github_app": None, "state_reason": None, "pinned_comment": None}
        if pull:
            issue["draft"] = False
            issue["pull_request"] = {"url": f"{API}/repos/{self.repo}/pulls/{n}", "html_url": issue["html_url"],
                                     "diff_url": f"{issue['html_url']}.diff", "patch_url": f"{issue['html_url']}.patch",
                                     "merged_at": None}
        self.issues[n] = issue
        return issue

    def _touch(self, n: int, at: float) -> None:
        self.issues[n]["updated_at"] = max(self.issues[n]["updated_at"], ts(at))

    def comment(self, n: int, login: str, at: float, *, bot: bool = False) -> dict:
        cid = next(self._ids)
        kind = "pull" if "pull_request" in self.issues[n] else "issues"
        c = {"url": f"{API}/repos/{self.repo}/issues/comments/{cid}",
             "html_url": f"https://github.com/{self.repo}/{kind}/{n}#issuecomment-{cid}",
             "issue_url": self._issue_url(n), "id": cid, "node_id": f"IC_{cid}", "user": user(login, bot),
             "created_at": ts(at), "updated_at": ts(at), "body": f"{CANARY} comment", "author_association": "MEMBER",
             "reactions": reactions(f"{API}/repos/{self.repo}/issues/comments/{cid}"),
             "performed_via_github_app": None, "minimized": None}
        self.comments.append(c)
        self.issues[n]["comments"] += 1
        self._touch(n, at)
        return c

    def event(self, n: int, kind: str, login: str, at: float, *, label: str | None = None, bot: bool = False) -> dict:
        eid = next(self._ids)
        if kind == "closed":
            self.issues[n].update(state="closed", closed_at=ts(at), state_reason="completed")
        if kind == "reopened":
            self.issues[n].update(state="open", closed_at=None, state_reason="reopened")
        if kind == "labeled":
            self.issues[n]["labels"].append({"name": label})
        e = {"id": eid, "node_id": f"E_{eid}", "url": f"{API}/repos/{self.repo}/issues/events/{eid}",
             "actor": user(login, bot), "event": kind, "commit_id": None, "commit_url": None, "created_at": ts(at),
             "issue": {k: v for k, v in json.loads(json.dumps(self.issues[n])).items() if k != "closed_by"},
             "performed_via_github_app": None, "intent": None}
        if kind == "labeled":
            e["label"] = {"name": label, "color": "ededed"}
        if kind == "closed":
            e["state_reason"] = "completed"
        self.events.append(e)
        self._touch(n, at)
        return e

    def edit_comment(self, c: dict, at: float) -> None:
        c["updated_at"] = ts(at)

    def list(self, what: str, q: dict[str, str]) -> list[dict]:
        since = q.get("since")
        desc = q.get("direction", "desc") == "desc"
        if what == "issues":
            items = [i for i in self.issues.values() if not since or i["updated_at"] >= since]
            if q.get("state", "open") != "all":
                items = [i for i in items if i["state"] == q.get("state", "open")]
            key = "updated_at" if q.get("sort") == "updated" else "created_at"
            return sorted(items, key=lambda i: (i[key], i["number"]), reverse=desc)
        if what == "comments":
            items = [c for c in self.comments if not since or c["updated_at"] >= since]
            key = "updated_at" if q.get("sort") == "updated" else "created_at"
            return sorted(items, key=lambda c: (c[key], c["id"]), reverse=q.get("direction", "asc") == "desc")
        return sorted(self.events, key=lambda e: (e["created_at"], e["id"]), reverse=True)

    def start(self) -> Self:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # quiet
                pass

            def _send(self, status: int, body, headers: dict | None = None):
                raw = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("X-RateLimit-Limit", "5000")
                self.send_header("X-RateLimit-Remaining", str(fake.remaining))
                self.send_header("X-RateLimit-Reset", "1793083020")
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                u = urllib.parse.urlsplit(self.path)
                q = dict(urllib.parse.parse_qsl(u.query))
                auth = self.headers.get("Authorization")
                fake.requests.append((u.path, q, auth))
                if auth not in (f"Bearer {TOKEN}", f"token {TOKEN}"):
                    return self._send(401, {"message": "Bad credentials", "documentation_url": "https://docs.github.com/rest",
                                            "status": "401"})
                if fake.fail_next:
                    status, headers = fake.fail_next.pop(0)
                    return self._send(status, {"message": "You have exceeded a secondary rate limit.",
                                               "documentation_url": "https://docs.github.com/rest"}, headers)
                fake.remaining -= 1
                parts = u.path.strip("/").split("/")
                if parts[:1] == ["repositories"] and parts[1:2] == [str(fake.repo_id)]:
                    rest = parts[2:]
                elif parts[:3] == ["repos", *fake.repo.split("/")]:
                    rest = parts[3:]
                else:
                    return self._send(404, {"message": "Not Found", "documentation_url": "https://docs.github.com/rest",
                                            "status": "404"})
                what = {("issues",): "issues", ("issues", "comments"): "comments", ("issues", "events"): "events"}.get(tuple(rest))
                if what is None:
                    return self._send(404, {"message": "Not Found", "status": "404"})
                items = fake.list(what, q)
                per = min(int(q.get("per_page", 30)), fake.per_page_max)
                page = int(q.get("page", 1))
                chunk = items[(page - 1) * per: page * per]
                headers = {}
                if page * per < len(items):
                    nq = urllib.parse.urlencode({**q, "page": page + 1})
                    last = urllib.parse.urlencode({**q, "page": (len(items) + per - 1) // per})
                    path = "/".join(["repositories", str(fake.repo_id), *rest])
                    headers["Link"] = f'<{fake.base}/{path}?{nq}>; rel="next", <{fake.base}/{path}?{last}>; rel="last"'
                return self._send(200, chunk, headers)

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
