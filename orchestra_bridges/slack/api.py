"""Slack's Web API, read-only: the three methods the bridge calls, the token kept in memory.

- ``auth.test``: whose token it is (the owner) and which workspace;
- ``search.messages``: paged by ``page`` (``count`` up to 100), newest first;
- ``reactions.list``: the owner's own reactions, the messages they reacted to most recently first
  (``full=true``, so every user is listed), paged by cursor.

The token goes in the ``Authorization`` header, only to the configured API's own scheme and host
(which ``config.py`` allows to be slack.com or a local fake only): a redirect anywhere else is
refused. A 429 (or ``ok: false`` with ``ratelimited``) is waited out for its ``Retry-After``, up to
``RETRIES`` times, when it asks for no more than ``max_wait`` (the bridge sets its poll interval);
a longer demand fails the call at once. The wait is ``sleep``, which the bridge makes its stop
event's ``wait``: a ``sleep`` that returns true (stop was asked for) fails the call instead of
retrying. Any other failure, including a body that is not JSON or is cut short, fails the call, and
the bridge tries again next round. Errors name the method and Slack's error code (or the kind of
failure), never the token, the body, or what was asked for."""

from __future__ import annotations

import http.client
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable

MAX_WAIT_S = 900.0
RETRIES = 3


class SlackError(Exception):
    def __init__(self, method: str, error: str):
        super().__init__(f"{method}: {error}")
        self.method, self.error = method, error


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
            raise SlackError("redirect", f"refused to {_origin(target)[1]}: the token goes only to the configured API")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _retry_after(headers: dict[str, str]) -> float:
    try:
        return max(float(headers.get("retry-after", "60")), 0.0)
    except ValueError:
        return 60.0


class Slack:
    def __init__(self, token: str, base: str = "https://slack.com/api", *, timeout: float = 30.0,
                 sleep: Callable[[float], object] = time.sleep, max_wait: float = MAX_WAIT_S):
        self._token = token
        self.base = base.rstrip("/")
        self.timeout = timeout
        self.sleep, self.max_wait = sleep, max_wait
        self.requests = 0
        self._opener = urllib.request.build_opener(_SameOrigin(self.base))

    def __repr__(self) -> str:                    # never the token
        return f"Slack({self.base!r})"

    def call(self, method: str, params: dict[str, str | int] | None = None) -> dict:
        url = f"{self.base}/{method}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        req = urllib.request.Request(url, headers={"Authorization": f"Bearer {self._token}",
                                                   "User-Agent": "orchestra-bridges"})
        for attempt in range(RETRIES + 1):
            self.requests += 1
            try:
                with self._opener.open(req, timeout=self.timeout) as r:
                    headers = {k.lower(): v for k, v in r.headers.items()}
                    body = json.loads(r.read() or b"{}")
            except urllib.error.HTTPError as e:
                headers = {k.lower(): v for k, v in (e.headers or {}).items()}
                if e.code != 429:
                    raise SlackError(method, f"HTTP {e.code}") from None
                body = {"ok": False, "error": "ratelimited"}
            except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException, ValueError) as e:
                raise SlackError(method, type(e).__name__) from None   # never the body: it may hold text
            if not isinstance(body, dict):
                raise SlackError(method, "not a JSON object")
            if body.get("ok"):
                return body
            error = str(body.get("error") or "unknown_error")
            if error != "ratelimited" or attempt == RETRIES:
                raise SlackError(method, error)
            wait = _retry_after(headers)
            if wait > self.max_wait:
                raise SlackError(method, f"ratelimited for {round(wait)} s")
            if self.sleep(wait):
                raise SlackError(method, "stopped")
        raise SlackError(method, "unreachable")       # pragma: no cover

    def auth(self) -> dict:
        """``auth.test``: ``user_id``, ``user`` (the handle), ``team_id``."""
        return self.call("auth.test")

    def search(self, query: str, page: int, count: int = 100) -> dict:
        """One page of ``search.messages``, newest first: the ``messages`` object."""
        body = self.call("search.messages", {"query": query, "sort": "timestamp", "sort_dir": "desc",
                                             "count": count, "page": page, "highlight": "false"})
        return body.get("messages") or {}

    def my_reactions(self, cursor: str = "", limit: int = 100) -> tuple[list[dict], str]:
        """One page of ``reactions.list`` for the token's owner: its items (``{type, channel,
        message: {ts, reactions: [{name, users, count}]}}``) and the cursor of the next page ("" at
        the end)."""
        params: dict[str, str | int] = {"full": "true", "limit": limit}
        if cursor:
            params["cursor"] = cursor
        body = self.call("reactions.list", params)
        return body.get("items") or [], str((body.get("response_metadata") or {}).get("next_cursor") or "")
