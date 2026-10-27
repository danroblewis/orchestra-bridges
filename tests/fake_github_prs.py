"""A fake of GitHub's REST API for pull requests, speaking the real shapes, for what the pull-request
bridge reads.

It serves ``/repos/<o>/<r>/pulls`` (``state``, ``sort``, ``direction``), ``/issues/<n>/timeline``,
``/pulls/<n>/comments``, ``/commits/<sha>``, ``/commits/<sha>/check-runs`` and
``/commits/<sha>/statuses``, paged with a Link header, demanding the bearer token. The test drives
it as people would act (``open``, ``push``, ``label``, ``review``, ``merge``, ...). Every object has
keys the real API's has (as of 2026-10; ``tests/contract`` compares them), and every title, body,
review, comment, diff hunk and commit message carries ``CANARY``, which must never leave the bridge.
As on GitHub, checks and statuses do not move a pull request's ``updated_at``, and a merge records
both ``merged`` and ``closed`` in the timeline."""

from __future__ import annotations

import hashlib
import itertools
import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Self

from fake_github import API, CANARY, TOKEN, reactions, ts, user


def sha_of(*parts) -> str:
    return hashlib.sha1(":".join(map(str, parts)).encode()).hexdigest()


class FakePRs:
    def __init__(self, repo: str = "acme/service", per_page_max: int = 100):
        self.repo, self.per_page_max = repo, per_page_max
        self.pulls: dict[int, dict] = {}
        self.timelines: dict[int, list[dict]] = {}
        self.line_comments: dict[int, list[dict]] = {}
        self.commits: dict[str, dict] = {}
        self.runs: dict[str, list[dict]] = {}
        self.statuses: dict[str, list[dict]] = {}
        self.requests: list[tuple[str, dict, str | None]] = []
        self.fail_next: list[int] = []
        self._ids = itertools.count(5000)
        self._server: ThreadingHTTPServer | None = None
        self.base = ""

    def _commit(self, sha: str, login: str | None, at: float) -> None:
        git_who = {"name": "Someone", "email": "someone@example.invalid", "date": ts(at)}
        self.commits[sha] = {
            "sha": sha, "node_id": f"C_{sha[:8]}", "url": f"{API}/repos/{self.repo}/commits/{sha}",
            "html_url": f"https://github.com/{self.repo}/commit/{sha}", "comments_url": f"{API}/repos/{self.repo}/commits/{sha}/comments",
            "commit": {"author": git_who, "committer": git_who, "message": f"{CANARY} commit message",
                       "tree": {"sha": sha_of("tree", sha), "url": ""}, "url": "", "comment_count": 0,
                       "verification": {"verified": False, "reason": "unsigned", "signature": None, "payload": None,
                                        "verified_at": None}},
            "author": user(login) if login else None, "committer": user(login) if login else None,
            "parents": [], "stats": {"total": 1, "additions": 1, "deletions": 0}, "files": []}

    def _touch(self, n: int, at: float) -> None:
        self.pulls[n]["updated_at"] = max(self.pulls[n]["updated_at"], ts(at))

    def _event(self, n: int, kind: str, login: str, at: float, **extra) -> dict:
        eid = next(self._ids)
        e = {"id": eid, "node_id": f"E_{eid}", "url": f"{API}/repos/{self.repo}/issues/events/{eid}", "actor": user(login),
             "event": kind, "commit_id": None, "commit_url": None, "created_at": ts(at), "performed_via_github_app": None,
             **extra}
        self.timelines[n].append(e)
        self._touch(n, at)
        return e

    def open(self, n: int, login: str, at: float, *, branch: str = "feature", labels: tuple[str, ...] = (),
             draft: bool = False, committer: str | None = None) -> dict:
        head = sha_of(n, branch, at)
        self._commit(head, committer, at - 5)
        url = f"{API}/repos/{self.repo}/pulls/{n}"
        repo = {"id": 1, "name": self.repo.split("/")[1], "full_name": self.repo}
        self.pulls[n] = {
            "url": url, "id": next(self._ids), "node_id": f"PR_{n}", "html_url": f"https://github.com/{self.repo}/pull/{n}",
            "diff_url": f"https://github.com/{self.repo}/pull/{n}.diff", "patch_url": f"https://github.com/{self.repo}/pull/{n}.patch",
            "issue_url": f"{API}/repos/{self.repo}/issues/{n}", "number": n, "state": "open", "locked": False,
            "title": f"{CANARY} title", "user": user(login), "body": f"{CANARY} body", "created_at": ts(at),
            "updated_at": ts(at), "closed_at": None, "merged_at": None, "merge_commit_sha": None, "assignees": [],
            "requested_reviewers": [], "requested_teams": [], "labels": [], "milestone": None, "draft": draft,
            "commits_url": f"{url}/commits", "review_comments_url": f"{url}/comments",
            "review_comment_url": f"{API}/repos/{self.repo}/pulls/comments{{/number}}",
            "comments_url": f"{API}/repos/{self.repo}/issues/{n}/comments", "statuses_url": f"{API}/repos/{self.repo}/statuses/{head}",
            "head": {"label": f"acme:{branch}", "ref": branch, "sha": head, "user": user("acme"), "repo": repo},
            "base": {"label": "acme:main", "ref": "main", "sha": sha_of("main"), "user": user("acme"), "repo": repo},
            "_links": {"self": {"href": url}}, "author_association": "MEMBER", "auto_merge": None, "assignee": None,
            "active_lock_reason": None}
        self.timelines[n] = [self._committed(head, at - 5)]
        self.line_comments[n] = []
        for name in labels:
            self.label(n, name, login, at)
        return self.pulls[n]

    def _committed(self, sha: str, at: float) -> dict:
        git_who = {"name": "Someone", "email": "someone@example.invalid", "date": ts(at)}
        return {"sha": sha, "node_id": f"C_{sha[:8]}", "url": f"{API}/repos/{self.repo}/git/commits/{sha}",
                "html_url": f"https://github.com/{self.repo}/commit/{sha}", "author": git_who, "committer": git_who,
                "tree": {"sha": sha_of("tree", sha), "url": ""}, "message": f"{CANARY} commit message", "parents": [],
                "verification": {"verified": False, "reason": "unsigned", "signature": None, "payload": None,
                                 "verified_at": None}, "event": "committed"}

    def head(self, n: int) -> str:
        return self.pulls[n]["head"]["sha"]

    def push(self, n: int, login: str | None, at: float, *, force: bool = False, linked: bool = True) -> str:
        """A new head. ``linked``: GitHub ties the commit to the pusher's account (its email is theirs)."""
        sha = sha_of(n, "push", at)
        self._commit(sha, login if linked else None, at - 2)
        self.pulls[n]["head"]["sha"] = sha
        self.timelines[n].append(self._committed(sha, at - 2))
        if force:
            self._event(n, "head_ref_force_pushed", login or "ghost", at, commit_id=sha)
        self._touch(n, at)
        return sha

    def label(self, n: int, name: str, login: str, at: float) -> None:
        self.pulls[n]["labels"].append({"id": next(self._ids), "node_id": "L", "url": "", "name": name, "color": "ededed",
                                        "default": False, "description": None})
        self._event(n, "labeled", login, at, label={"name": name, "color": "ededed"})

    def unlabel(self, n: int, name: str, login: str, at: float) -> None:
        self.pulls[n]["labels"] = [lb for lb in self.pulls[n]["labels"] if lb["name"] != name]
        self._event(n, "unlabeled", login, at, label={"name": name, "color": "ededed"})

    def request_review(self, n: int, requester: str, reviewer: str, at: float) -> None:
        self.pulls[n]["requested_reviewers"].append(user(reviewer))
        self._event(n, "review_requested", requester, at, review_requester=user(requester), requested_reviewer=user(reviewer))

    def review(self, n: int, login: str, at: float, state: str, comments: tuple[tuple[str, int], ...] = ()) -> int:
        """A submitted review: ``state`` APPROVED, CHANGES_REQUESTED or COMMENTED, with line comments."""
        rid = next(self._ids)
        head = self.head(n)
        self.pulls[n]["requested_reviewers"] = [u for u in self.pulls[n]["requested_reviewers"] if u["login"] != login]
        self.timelines[n].append({
            "id": rid, "node_id": f"PRR_{rid}", "user": user(login), "body": f"{CANARY} review", "commit_id": head,
            "submitted_at": ts(at), "updated_at": ts(at), "state": state.lower(),
            "html_url": f"https://github.com/{self.repo}/pull/{n}#pullrequestreview-{rid}",
            "pull_request_url": self.pulls[n]["url"], "author_association": "MEMBER",
            "_links": {"html": {"href": ""}, "pull_request": {"href": ""}}, "event": "reviewed"})
        for path, line in comments:
            cid = next(self._ids)
            self.line_comments[n].append({
                "url": f"{API}/repos/{self.repo}/pulls/comments/{cid}", "pull_request_review_id": rid, "id": cid,
                "node_id": f"PRRC_{cid}", "diff_hunk": f"@@ {CANARY} hunk", "path": path, "commit_id": head,
                "original_commit_id": head, "user": user(login), "body": f"{CANARY} line comment",
                "created_at": ts(at - 30), "updated_at": ts(at), "html_url": "", "pull_request_url": self.pulls[n]["url"],
                "author_association": "MEMBER", "_links": {}, "reactions": reactions(""), "start_line": None,
                "original_start_line": None, "start_side": None, "line": line, "original_line": line, "side": "RIGHT",
                "original_position": line + 1, "position": line + 1, "subject_type": "line"})
        self._touch(n, at)
        return rid

    def comment(self, n: int, login: str, at: float) -> int:
        cid = next(self._ids)
        self.timelines[n].append({
            "url": f"{API}/repos/{self.repo}/issues/comments/{cid}", "html_url": "", "issue_url": self.pulls[n]["issue_url"],
            "id": cid, "node_id": f"IC_{cid}", "user": user(login), "created_at": ts(at), "updated_at": ts(at),
            "author_association": "MEMBER", "body": f"{CANARY} comment", "reactions": reactions(""),
            "performed_via_github_app": None, "event": "commented", "actor": user(login)})
        self._touch(n, at)
        return cid

    def check(self, n: int, name: str, conclusion: str | None, at: float, app: str = "github-actions") -> int:
        """A check run on the head; ``conclusion`` None: still running."""
        rid = next(self._ids)
        self.runs.setdefault(self.head(n), []).append({
            "id": rid, "name": name, "node_id": f"CR_{rid}", "head_sha": self.head(n), "external_id": "", "url": "",
            "html_url": "", "details_url": "", "status": "completed" if conclusion else "in_progress",
            "conclusion": conclusion, "started_at": ts(at - 60), "completed_at": ts(at) if conclusion else None,
            "output": {"title": f"{CANARY}", "summary": f"{CANARY}", "text": None, "annotations_count": 0,
                       "annotations_url": ""},
            "check_suite": {"id": 1}, "app": {"id": 15368, "slug": app, "name": app, "owner": user("github")},
            "pull_requests": []})
        return rid

    def status(self, n: int, context: str, state: str, login: str, at: float) -> int:
        sid = next(self._ids)
        self.statuses.setdefault(self.head(n), []).insert(0, {
            "url": "", "avatar_url": "", "id": sid, "node_id": f"SC_{sid}", "state": state,
            "description": f"{CANARY} status", "target_url": None, "context": context, "created_at": ts(at),
            "updated_at": ts(at), "creator": user(login)})
        return sid

    def merge(self, n: int, login: str, at: float) -> None:
        p = self.pulls[n]
        p.update(state="closed", merged_at=ts(at), closed_at=ts(at), merge_commit_sha=sha_of("merge", n))
        self._event(n, "merged", login, at, commit_id=p["merge_commit_sha"])
        self._event(n, "closed", login, at, commit_id=p["merge_commit_sha"], state_reason=None)

    def close(self, n: int, login: str, at: float) -> None:
        self.pulls[n].update(state="closed", closed_at=ts(at))
        self._event(n, "closed", login, at, state_reason=None)

    def reopen(self, n: int, login: str, at: float) -> None:
        self.pulls[n].update(state="open", closed_at=None)
        self._event(n, "reopened", login, at)

    def list_pulls(self, q: dict[str, str]) -> list[dict]:
        state = q.get("state", "open")
        items = [p for p in self.pulls.values() if state == "all" or p["state"] == state]
        key = "updated_at" if q.get("sort") == "updated" else "created_at"
        return sorted(items, key=lambda p: (p[key], p["number"]), reverse=q.get("direction", "desc") == "desc")

    def route(self, rest: list[str], q: dict[str, str]) -> object | None:
        if rest == ["pulls"]:
            return self.list_pulls(q)
        if len(rest) == 3 and rest[0] == "issues" and rest[2] == "timeline" and int(rest[1]) in self.timelines:
            return self.timelines[int(rest[1])]
        if len(rest) == 3 and rest[0] == "pulls" and rest[2] == "comments" and int(rest[1]) in self.line_comments:
            return self.line_comments[int(rest[1])]
        if len(rest) == 2 and rest[0] == "commits" and rest[1] in self.commits:
            return self.commits[rest[1]]
        if len(rest) == 3 and rest[0] == "commits" and rest[2] == "check-runs":
            runs = self.runs.get(rest[1], [])
            return {"total_count": len(runs), "check_runs": runs}
        if len(rest) == 3 and rest[0] == "commits" and rest[2] == "statuses":
            return self.statuses.get(rest[1], [])
        return None

    def start(self) -> Self:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # quiet
                pass

            def _send(self, status: int, body, headers: dict | None = None):
                raw = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("X-RateLimit-Remaining", "4999")
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
                if auth != f"Bearer {TOKEN}":
                    return self._send(401, {"message": "Bad credentials", "status": "401"})
                if fake.fail_next:
                    return self._send(fake.fail_next.pop(0), {"message": "Server Error"})
                parts = u.path.strip("/").split("/")
                body = fake.route(parts[3:], q) if parts[:3] == ["repos", *fake.repo.split("/")] else None
                if body is None:
                    return self._send(404, {"message": "Not Found", "status": "404"})
                if not isinstance(body, list):
                    return self._send(200, body)
                per = min(int(q.get("per_page", 30)), fake.per_page_max)
                page = int(q.get("page", 1))
                headers = {}
                if page * per < len(body):
                    headers["Link"] = f'<{fake.base}{u.path}?{urllib.parse.urlencode({**q, "page": page + 1})}>; rel="next"'
                return self._send(200, body[(page - 1) * per: page * per], headers)

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
