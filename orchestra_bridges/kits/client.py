"""A cached, rate-limited HTTP client for a machine that reads another system's API, over the
standard library.

``CachedClient(base_url, token_env=None, token_file=None, per_minute=60, cache_dir=..., ttl_s=300)``:

- **GET and POST** (``get``, ``post``) to paths under ``base_url``, answers parsed as JSON. A
  request to another origin is refused, so the token goes only to the configured host.
- **A TTL cache on disk** (``cache.py``) keyed by method, URL and body, of **only what ``keep``
  returns**: ``keep`` (per client or per request) is applied to an answer before it is cached or
  returned, and a request without one is never cached. A poller keeps metadata only (ids, states,
  times), so no title or body is ever written to disk (the bridges' rule 1).
- **A per-host rate limit**: Orchestra's own token bucket (``orchestra.sources.limits.RateLimit``),
  in a file every process on this machine shares for the host (by default beside the buckets of
  Orchestra's ``[[sources]]``, ``~/.orchestrator/stores/<host key>/kits.rate``).
- **Retries** on 429 and 5xx and on a dropped connection, with a doubling backoff; a
  ``Retry-After`` is honoured (and holds the host's bucket for every reader) up to ``max_wait_s``.
- **The token stays in memory**: read once, from the environment variable ``token_env`` or the
  file ``token_file`` (outside any repository, ``chmod 600``), sent as ``auth_format`` in
  ``Authorization``. It is never logged, never in a cache key or file, never in an error or repr.
- **``dry_run``**: answers only from the cache, of any age, and raises ``NotCached`` on a miss: a
  rehearsal that sends nothing."""

from __future__ import annotations

import email.utils
import ipaddress
import json
import logging
import os
import re
import stat
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

from orchestra.sources.limits import RateLimit, store_dir, store_key

from .cache import DiskCache, key

log = logging.getLogger("orchestra_bridges.kits.client")

RETRY_STATUS = {429, 500, 502, 503, 504}
SECRET_PARAM = re.compile(r"(?i)token|key|secret|pass|auth|sig")


class KitError(Exception):
    pass


class TokenMissing(KitError):
    pass


class NotCached(KitError):
    """A dry run asked for an answer the cache does not hold."""


class HTTPFailure(KitError):
    def __init__(self, status: int, url: str):
        super().__init__(f"HTTP {status} from {url}")
        self.status, self.url = status, url


def redact(url: str) -> str:
    """``url`` with the value of any query parameter named like a secret replaced."""
    u = urllib.parse.urlsplit(url)
    if not u.query:
        return url
    q = [(k, "REDACTED" if SECRET_PARAM.search(k) else v) for k, v in urllib.parse.parse_qsl(u.query, keep_blank_values=True)]
    return urllib.parse.urlunsplit(u._replace(query=urllib.parse.urlencode(q)))


def _loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == "localhost"


def read_token(token_env: str | None, token_file: str | Path | None) -> str | None:
    """The token from ``$token_env``, else from ``token_file``; None when neither is named. Errors
    name where it looked, never what it found."""
    if token_env:
        if v := os.environ.get(token_env, "").strip():
            return v
        if not token_file:
            raise TokenMissing(f"the environment variable {token_env} is not set")
    if not token_file:
        return None
    p = Path(token_file).expanduser()
    if not p.is_file():
        raise TokenMissing(f"no token file at {p}" + (f" and no {token_env}" if token_env else ""))
    if p.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise TokenMissing(f"{p} is readable by others: chmod 600 it")
    if not (v := p.read_text(encoding="utf-8").strip()):
        raise TokenMissing(f"{p} is empty")
    return v


def default_cache_dir() -> Path:
    """``<state>/kits/cache`` of the project at ORCHESTRA_REPO (else the working directory), or
    under Orchestra's home when there is no project."""
    from orchestra.config import ConfigError, home, load_project
    try:
        return load_project().state_dir / "kits" / "cache"
    except (ConfigError, OSError):
        return home() / "kits" / "cache"


def retry_after(value: str | None, now: float) -> float | None:
    """Seconds a ``Retry-After`` asks for: a number, or an HTTP date."""
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        return max(0.0, email.utils.parsedate_to_datetime(value).timestamp() - now)
    except (TypeError, ValueError):
        return None


class CachedClient:
    def __init__(self, base_url: str, token_env: str | None = None, token_file: str | Path | None = None,
                 per_minute: int = 60, cache_dir: str | Path | None = None, ttl_s: float = 300.0, *,
                 auth_format: str = "Bearer {token}", headers: dict[str, str] | None = None,
                 keep: Callable[[Any], Any] | None = None, dry_run: bool = False, retries: int = 3,
                 backoff_s: float = 1.0, max_wait_s: float = 300.0, timeout_s: float = 30.0,
                 rate_dir: str | Path | None = None, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.time):
        u = urllib.parse.urlsplit(base_url)
        if u.scheme not in ("http", "https") or not u.hostname:
            raise ValueError(f"base_url must be an http(s) URL, not {base_url!r}")
        self.base_url = base_url.rstrip("/")
        self.origin = f"{u.scheme}://{u.netloc}".lower()
        self.host = (u.hostname or "").lower()
        self._token = read_token(token_env, token_file)
        if self._token and u.scheme != "https" and not _loopback(self.host):
            raise ValueError(f"{self.origin}: a token goes only over https (or to a fake on this machine)")
        self.auth_format = auth_format
        self.headers = dict(headers or {})
        self.keep, self.dry_run, self.ttl_s = keep, dry_run, ttl_s
        self.retries, self.backoff_s, self.max_wait_s, self.timeout_s = retries, backoff_s, max_wait_s, timeout_s
        self.cache = DiskCache(Path(cache_dir).expanduser() if cache_dir else default_cache_dir(), clock=clock)
        bucket = Path(rate_dir) if rate_dir else store_dir(store_key(self.origin))
        self.limit = RateLimit(per_minute / 60.0, max(1.0, per_minute / 4.0), bucket / "kits.rate",
                               what=f"the host {self.host}", sleep=sleep, clock=clock)
        self._sleep, self._clock = sleep, clock
        self.sent = 0                       # requests that reached the network, for tests and logs
        self.hits = 0

    @classmethod
    def for_project(cls, project, base_url: str, system: str | None = None, token_env: str | None = None,
                    **kw) -> CachedClient:
        """A client with ``[bridges.kits]``'s cache directory and TTL unless given, the host's rate
        (or ``per_minute``, when lower) and the system's token variable (``tokens.<system>``) unless given."""
        from .settings import cache_dir, read
        s = read(project.config.raw)
        host = (urllib.parse.urlsplit(base_url).hostname or "").lower()
        kw.setdefault("cache_dir", cache_dir(s, project.state_dir))
        kw.setdefault("ttl_s", s.ttl_s)
        limit = s.per_minute_for(host)          # the project's limit for the host; a machine may ask for less
        kw["per_minute"] = min(int(kw.get("per_minute") or limit), limit)
        return cls(base_url, token_env=token_env or (s.token_env(system) if system else None), **kw)

    def __repr__(self) -> str:
        return f"CachedClient({self.base_url!r}, token={'set' if self._token else 'none'})"

    def url(self, path: str, params: dict | None = None) -> str:
        if path.startswith(("http://", "https://")):
            u = urllib.parse.urlsplit(path)
            if f"{u.scheme}://{u.netloc}".lower() != self.origin:
                raise ValueError(f"{redact(path)} is not on {self.origin}: this client sends nowhere else")
            full = path
        else:
            full = f"{self.base_url}/{path.lstrip('/')}" if path else self.base_url
        if params:
            items = [(k, x) for k, v in params.items() if v is not None for x in (v if isinstance(v, list | tuple) else [v])]
            full += ("&" if "?" in full else "?") + urllib.parse.urlencode(items)
        return full

    def get(self, path: str, params: dict | None = None, **kw) -> Any:
        return self.request("GET", path, params, **kw)

    def post(self, path: str, json_body: Any = None, *, form: dict | None = None, params: dict | None = None,
             **kw) -> Any:
        if form is not None:
            body = urllib.parse.urlencode(list(form.items()), doseq=True).encode()
            ctype = "application/x-www-form-urlencoded"
        else:
            body = json.dumps(json_body, sort_keys=True).encode() if json_body is not None else b""
            ctype = "application/json"
        return self.request("POST", path, params, body=body, content_type=ctype, **kw)

    def request(self, method: str, path: str, params: dict | None = None, *, body: bytes | None = None,
                content_type: str | None = None, keep: Callable[[Any], Any] | None = None,
                ttl_s: float | None = None, cache: bool = True, headers: dict[str, str] | None = None) -> Any:
        url = self.url(path, params)
        k = key(method, url, body)
        ttl = self.ttl_s if ttl_s is None else ttl_s
        keep = keep or self.keep
        cache = cache and keep is not None           # only what a caller keeps reaches the disk
        if cache or self.dry_run:
            hit = self.cache.get(k, None if self.dry_run else ttl)
            if hit is not None:
                self.hits += 1
                log.debug("cache hit %s %s", method, redact(url))
                return hit["value"]
        if self.dry_run:
            raise NotCached(f"dry run: {method} {redact(url)} is not cached")
        status, value = self._send(method, url, body, content_type, headers)
        value = keep(value) if keep else value
        if cache and ttl > 0:
            self.cache.put(k, status, value)
        return value

    def _headers(self, content_type: str | None, extra: dict[str, str] | None) -> dict[str, str]:
        h = {"Accept": "application/json", "User-Agent": "orchestra-bridges-kits", **self.headers, **(extra or {})}
        if content_type:
            h["Content-Type"] = content_type
        if self._token:
            h["Authorization"] = self.auth_format.format(token=self._token)
        return h

    def _send(self, method: str, url: str, body: bytes | None, content_type: str | None,
              extra: dict[str, str] | None) -> tuple[int, Any]:
        shown = redact(url)
        for attempt in range(self.retries + 1):
            self.limit.take(where=shown)
            req = urllib.request.Request(url, data=body if method != "GET" else None, method=method,
                                         headers=self._headers(content_type, extra))
            wait: float | None = None
            try:
                self.sent += 1
                with urllib.request.urlopen(req, timeout=self.timeout_s) as r:
                    raw = r.read()
                    log.info("%s %s -> %s", method, shown, r.status)
                    return r.status, _parse(raw)
            except urllib.error.HTTPError as e:
                e.read()
                log.info("%s %s -> %s", method, shown, e.code)
                if e.code not in RETRY_STATUS or attempt == self.retries:
                    raise HTTPFailure(e.code, shown) from None
                wait = retry_after(e.headers.get("Retry-After"), self._clock())
                if wait is not None:
                    if wait > self.max_wait_s:
                        raise HTTPFailure(e.code, shown) from None
                    self.limit.hold(wait)
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                log.info("%s %s -> %s", method, shown, type(e).__name__)
                if attempt == self.retries:
                    raise KitError(f"{method} {shown}: {type(e).__name__}") from None
            if wait is None:            # a Retry-After is waited out by the bucket's hold, at the next take
                self._sleep(min(self.max_wait_s, self.backoff_s * 2 ** attempt))
        raise KitError(f"{method} {shown}: no answer")    # not reached


def _parse(raw: bytes) -> Any:
    if not raw:
        return None
    try:
        return json.loads(raw)
    except ValueError:
        return raw.decode("utf-8", "replace")
