"""The scripted team's HTTP sender: JSON to one origin, the ``Authorization`` it was given (or none,
for a fake on this machine), every write (anything but GET) at most once every ``min_interval_s``
(2 seconds by default, across every system the throttle is shared with), a 429 waited out
(``Retry-After``) and tried again. The credential is never logged, printed or put in an error."""

from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from typing import Any

WRITE_INTERVAL_S = 2.0


class Throttle:
    """At most one write every ``min_interval_s``: ``wait()`` sleeps until the next is allowed."""

    def __init__(self, min_interval_s: float = WRITE_INTERVAL_S, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        self.min_interval_s, self.clock, self.sleep = min_interval_s, clock, sleep
        self._last: float | None = None
        self.writes = 0

    def wait(self) -> None:
        if self._last is not None:
            gap = self._last + self.min_interval_s - self.clock()
            if gap > 0:
                self.sleep(gap)
        self._last = self.clock()
        self.writes += 1


class WireError(Exception):
    def __init__(self, status: int, method: str, path: str, detail: str = ""):
        super().__init__(f"{method} {path}: HTTP {status}{': ' + detail if detail else ''}")
        self.status = status


def _detail(raw: bytes) -> str:
    """A short reason from an error's body (Jira's ``errorMessages``/``errors``, Discord's ``message``)."""
    try:
        b = json.loads(raw)
    except ValueError:
        return ""
    if not isinstance(b, dict):
        return ""
    parts = [*(b.get("errorMessages") or []), *(f"{k}: {v}" for k, v in (b.get("errors") or {}).items())]
    if b.get("message"):
        parts.append(str(b["message"]))
    return "; ".join(str(p) for p in parts)[:200]


class Wire:
    def __init__(self, base_url: str, auth: str | None, throttle: Throttle, user_agent: str = "orchestra-team",
                 timeout_s: float = 30.0, retries: int = 3):
        self.base_url, self._auth, self.throttle = base_url.rstrip("/"), auth, throttle
        self.user_agent, self.timeout_s, self.retries = user_agent, timeout_s, retries

    def __repr__(self) -> str:
        return f"Wire({self.base_url!r}, auth={'set' if self._auth else 'none'})"

    def call(self, method: str, path: str, body: Any = None, query: dict | None = None) -> Any:
        """One request; the answer's JSON (None when empty). ``WireError`` on a status ≥ 400."""
        if "://" in path:
            raise ValueError("a path on the configured origin, never a URL")
        url = f"{self.base_url}/{path.lstrip('/')}" + (f"?{urllib.parse.urlencode(query)}" if query else "")
        headers = {"Accept": "application/json", "User-Agent": self.user_agent}
        if body is not None:
            headers["Content-Type"] = "application/json"
        if self._auth:
            headers["Authorization"] = self._auth
        for attempt in range(self.retries + 1):
            if method != "GET":
                self.throttle.wait()
            req = urllib.request.Request(url, method=method, headers=headers,
                                         data=None if body is None else json.dumps(body).encode())
            try:
                with urllib.request.urlopen(req, timeout=self.timeout_s) as r:
                    raw = r.read()
                return json.loads(raw) if raw.strip() else None
            except urllib.error.HTTPError as e:
                if e.code == 429 and attempt < self.retries:
                    self.throttle.sleep(min(60.0, float(e.headers.get("Retry-After") or 2)))
                    continue
                raise WireError(e.code, method, path.split("?", 1)[0], _detail(e.read())) from None
        raise AssertionError("unreachable")
