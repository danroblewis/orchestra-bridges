"""GitHub's REST API, read-only: paged lists, the rate limits honoured, the token kept in memory.

The token is the owner's ``gh`` login for the configured API's host, as ``gh`` itself finds it:
for github.com, ``GH_TOKEN`` (or ``GITHUB_TOKEN``) when set; for a GitHub Enterprise host,
``GH_ENTERPRISE_TOKEN`` (or ``GITHUB_ENTERPRISE_TOKEN``); else what ``gh auth token --hostname
<host>`` prints at start, so an Enterprise API never gets the github.com token. It is never
written, logged or published, and it is sent only to the configured API's own scheme and host: a
pagination link or a redirect that points anywhere else (another host, https to http) is not
followed.

Limits honoured: a ``Retry-After`` (secondary limits, 429) is waited out; a primary limit run out
(``X-RateLimit-Remaining: 0``) waits until ``X-RateLimit-Reset`` when that is within
``MAX_WAIT_S``, and otherwise fails the poll, which the next round tries again."""

from __future__ import annotations

import json
import os
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Iterator

MAX_WAIT_S = 900.0
RETRIES = 3
PER_PAGE = 100


class GitHubError(Exception):
    pass


def gh_host(api: str) -> str:
    """The host ``gh`` knows the API by: github.com for api.github.com, else the API's own host."""
    host = (urllib.parse.urlsplit(api).hostname or "").lower()
    return "github.com" if host in ("api.github.com", "github.com") else host


def gh_token(api: str = "https://api.github.com") -> str:
    """The owner's token for ``api``'s host: the environment's, else ``gh auth token --hostname``'s.
    Raises when there is none."""
    host = gh_host(api)
    names = ("GH_TOKEN", "GITHUB_TOKEN") if host == "github.com" else ("GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN")
    token = next((os.environ[n] for n in names if os.environ.get(n)), None)
    if token:
        return token
    try:
        p = subprocess.run(["gh", "auth", "token", "--hostname", host], capture_output=True, text=True, timeout=30, check=False)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise GitHubError(f"no {names[0]} and `gh auth token --hostname {host}` could not run: {type(e).__name__}") from e
    if p.returncode != 0 or not p.stdout.strip():
        raise GitHubError(f"no {names[0]} and `gh auth token --hostname {host}` gave none: run `gh auth login --hostname {host}`")
    return p.stdout.strip()


def _origin(url: str) -> tuple[str, str]:
    u = urllib.parse.urlsplit(url)
    return u.scheme.lower(), u.netloc.lower()


class _SameOrigin(urllib.request.HTTPRedirectHandler):
    """Follows a redirect only to the configured API's scheme and host: urllib's default copies the
    Authorization header to wherever a redirect points."""

    def __init__(self, base: str):
        self.base = base

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = urllib.parse.urljoin(req.full_url, newurl)
        if _origin(target) != _origin(self.base):
            raise GitHubError(f"refusing to follow a redirect to {_origin(target)[1]}: the token goes only to the configured API")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _next_link(header: str | None) -> str | None:
    """The ``rel="next"`` URL of a Link header."""
    for part in (header or "").split(","):
        url, _, rel = part.partition(";")
        if 'rel="next"' in rel:
            return url.strip().strip("<>")
    return None


class GitHub:
    def __init__(self, token: str, base: str = "https://api.github.com", *, timeout: float = 30.0,
                 sleep: Callable[[float], None] = time.sleep, clock: Callable[[], float] = time.time):
        self._token = token
        self.base = base.rstrip("/")
        self.timeout = timeout
        self.sleep, self.clock = sleep, clock
        self.requests = 0
        self.rate_remaining: int | None = None
        self._opener = urllib.request.build_opener(_SameOrigin(self.base))

    def __repr__(self) -> str:                    # never the token
        return f"GitHub({self.base!r})"

    def _ours(self, url: str) -> bool:
        return _origin(url) == _origin(self.base)

    def _get(self, url: str) -> tuple[object, dict[str, str]]:
        if not self._ours(url):
            raise GitHubError(f"refusing to send the token to {urllib.parse.urlsplit(url).netloc}")
        req = urllib.request.Request(url, headers={
            "Authorization": f"Bearer {self._token}", "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28", "User-Agent": "orchestra-bridges"})
        for attempt in range(RETRIES + 1):
            self.requests += 1
            try:
                with self._opener.open(req, timeout=self.timeout) as r:
                    headers = {k.lower(): v for k, v in r.headers.items()}
                    self._note(headers)
                    return json.loads(r.read() or b"null"), headers
            except urllib.error.HTTPError as e:
                headers = {k.lower(): v for k, v in (e.headers or {}).items()}
                self._note(headers)
                wait = self._wait_for(e.code, headers)
                if wait is None or attempt == RETRIES:
                    raise GitHubError(f"GET {urllib.parse.urlsplit(url).path}: HTTP {e.code}") from None
                self.sleep(wait)
            except (urllib.error.URLError, TimeoutError, OSError) as e:
                raise GitHubError(f"GET {urllib.parse.urlsplit(url).path}: {type(e).__name__}") from None
        raise GitHubError("unreachable")              # pragma: no cover

    def _note(self, headers: dict[str, str]) -> None:
        if "x-ratelimit-remaining" in headers:
            try:
                self.rate_remaining = int(headers["x-ratelimit-remaining"])
            except ValueError:
                pass

    def _wait_for(self, code: int, headers: dict[str, str]) -> float | None:
        """How long to wait before trying again, or None to fail now."""
        if code not in (403, 429):
            return None
        if "retry-after" in headers:
            try:
                return min(float(headers["retry-after"]), MAX_WAIT_S)
            except ValueError:
                return 60.0
        if headers.get("x-ratelimit-remaining") == "0" and "x-ratelimit-reset" in headers:
            wait = float(headers["x-ratelimit-reset"]) - self.clock() + 1
            return max(wait, 1.0) if wait <= MAX_WAIT_S else None
        return 60.0 if code == 429 else None

    def pages(self, path: str, params: dict[str, str | int]) -> Iterator[list[dict]]:
        """Each page of a list endpoint (``path`` under the API, e.g. ``/repos/o/r/issues``), in the
        order GitHub serves them, following the Link header."""
        url: str | None = f"{self.base}{path}?{urllib.parse.urlencode({**params, 'per_page': PER_PAGE})}"
        while url:
            body, headers = self._get(url)
            if not isinstance(body, list):
                raise GitHubError(f"GET {path}: expected a list, got {type(body).__name__}")
            yield body
            url = _next_link(headers.get("link"))
