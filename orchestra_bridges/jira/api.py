"""Jira's REST API, read-only: Data Center (``/rest/api/2``, the owner's) or Cloud (``/rest/api/3``),
paged searches, ``Retry-After`` honoured, the token kept in memory.

**Credentials.** Data Center takes a personal access token (``Authorization: Bearer``). Cloud takes
an API token with the email of the account it belongs to (HTTP Basic, as Atlassian asks). Where the
token comes from is ``secret.py``. The token is never written, logged or published, and is sent
only to the configured site's scheme and host: a redirect anywhere else is refused, and so is a
request built for another origin.

**Searches.** Data Center's ``/search`` pages with ``startAt`` until ``total``; Cloud's
``/search/jql`` pages with ``nextPageToken`` until ``isLast``. Both yield the same issue objects.

**Limits.** A 429 or 503 waits its ``Retry-After`` (seconds), at most ``max_wait``, and tries again,
up to ``RETRIES`` times; one without the header waits a minute. Errors name the path and the HTTP
code, never a body (a body may hold text)."""

from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator

MAX_WAIT_S = 900.0
RETRIES = 3
PAGE = 50


class JiraError(Exception):
    pass


def _origin(url: str) -> tuple[str, str]:
    u = urllib.parse.urlsplit(url)
    return u.scheme.lower(), u.netloc.lower()


class _SameOrigin(urllib.request.HTTPRedirectHandler):
    """Follows a redirect only to the site's own scheme and host: urllib's default would carry the
    Authorization header wherever a redirect points."""

    def __init__(self, base: str):
        self.base = base

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urllib.parse.urljoin(req.full_url, newurl)
        if _origin(target) != _origin(self.base):
            raise JiraError(f"refusing to follow a redirect to {_origin(target)[1]}: the token goes only to the configured site")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


class Jira:
    def __init__(self, base: str, token: str, *, deployment: str = "datacenter", email: str | None = None,
                 timeout: float = 30.0, sleep: Callable[[float], None] = time.sleep, max_wait: float = MAX_WAIT_S):
        if deployment == "cloud" and not email:
            raise JiraError("Jira Cloud needs the email of the account the API token belongs to (JIRA_EMAIL or email)")
        self.base = base.rstrip("/")
        self.cloud = deployment == "cloud"
        self.prefix = "/rest/api/3" if self.cloud else "/rest/api/2"
        basic = base64.b64encode(f"{email}:{token}".encode()).decode() if self.cloud else None
        self._auth = f"Basic {basic}" if basic else f"Bearer {token}"
        self.timeout, self.sleep, self.max_wait = timeout, sleep, max_wait
        self.requests = 0
        self.waited: list[float] = []
        self._opener = urllib.request.build_opener(_SameOrigin(self.base))

    def __repr__(self) -> str:                    # never the token
        return f"Jira({self.base!r}, {'cloud' if self.cloud else 'datacenter'})"

    def get(self, path: str, params: dict[str, str | int] | None = None) -> object:
        """One GET of ``path`` under the API (``/myself``), its JSON body."""
        query = f"?{urllib.parse.urlencode(params)}" if params else ""
        url = f"{self.base}{self.prefix}{path}{query}"
        if _origin(url) != _origin(self.base):
            raise JiraError("refusing to send the token to another origin")
        req = urllib.request.Request(url, headers={"Authorization": self._auth, "Accept": "application/json",
                                                   "User-Agent": "orchestra-bridges"})
        for attempt in range(RETRIES + 1):
            self.requests += 1
            try:
                with self._opener.open(req, timeout=self.timeout) as r:
                    return json.loads(r.read() or b"null")
            except urllib.error.HTTPError as e:
                wait = self._wait_for(e.code, e.headers.get("Retry-After") if e.headers else None)
                if wait is None or attempt == RETRIES:
                    raise JiraError(f"GET {self.prefix}{path}: HTTP {e.code}") from None
                self.waited.append(wait)
                self.sleep(wait)
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                raise JiraError(f"GET {self.prefix}{path}: {type(e).__name__}") from None
        raise JiraError("unreachable")                # pragma: no cover

    def _wait_for(self, code: int, retry_after: str | None) -> float | None:
        if code not in (429, 503):
            return None
        try:
            return min(max(float(retry_after), 0.0), self.max_wait) if retry_after else min(60.0, self.max_wait)
        except ValueError:                        # an HTTP date: wait a minute
            return min(60.0, self.max_wait)

    def search(self, jql: str, fields: list[str], *, changelog: bool = False, page: int = PAGE) -> Iterator[dict]:
        """Every issue ``jql`` matches, with ``fields`` (and the changelog), page by page."""
        params: dict[str, str | int] = {"jql": jql, "fields": ",".join(fields), "maxResults": page}
        if changelog:
            params["expand"] = "changelog"
        if self.cloud:
            token: str | None = None
            while True:
                body = self._object("/search/jql", {**params, **({"nextPageToken": token} if token else {})})
                yield from body.get("issues") or []
                token = body.get("nextPageToken")
                if body.get("isLast", not token) or not token:
                    return
        start = 0
        while True:
            body = self._object("/search", {**params, "startAt": start})
            issues = body.get("issues") or []
            yield from issues
            start += len(issues)
            if not issues or start >= int(body.get("total") or 0):
                return

    def _object(self, path: str, params: dict | None = None) -> dict:
        body = self.get(path, params)
        if not isinstance(body, dict):
            raise JiraError(f"GET {self.prefix}{path}: expected an object, got {type(body).__name__}")
        return body

    def listing(self, path: str) -> list:
        body = self.get(path)
        if not isinstance(body, list):
            raise JiraError(f"GET {self.prefix}{path}: expected a list, got {type(body).__name__}")
        return body

    def changelog(self, key: str) -> list[dict]:
        """An issue's whole changelog, when a search held only part of it: Cloud pages
        ``/issue/<key>/changelog``; Data Center gives it whole with the issue."""
        if not self.cloud:
            body = self._object(f"/issue/{urllib.parse.quote(key)}", {"expand": "changelog", "fields": "created"})
            return list((body.get("changelog") or {}).get("histories") or [])
        out: list[dict] = []
        while True:
            body = self._object(f"/issue/{urllib.parse.quote(key)}/changelog", {"startAt": len(out), "maxResults": 100})
            values = body.get("values") or []
            out += values
            if not values or body.get("isLast", True) or len(out) >= int(body.get("total") or 0):
                return out

    def comments(self, key: str) -> list[dict]:
        """An issue's comments, oldest first, when a search held only part of them."""
        out: list[dict] = []
        while True:
            body = self._object(f"/issue/{urllib.parse.quote(key)}/comment",
                                {"startAt": len(out), "maxResults": 100, "orderBy": "created"})
            got = body.get("comments") or []
            out += got
            if not got or len(out) >= int(body.get("total") or 0):
                return out
