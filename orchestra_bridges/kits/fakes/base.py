"""What every kit fake shares: a stdlib HTTP server on a random free loopback port, seeded from a
small JSON fixture, that records each request it receives (method, path, query, body, and whether
it carried the expected token: never the token itself), checks the token when it was given one, and
can be told to fail the next requests (``fail_next(429, {"Retry-After": "1"})``).

A fake's ``route(method, path, query, body)`` returns ``(status, body)``, ``body`` a JSON-able value
(or None for an empty answer). Use it as a context manager, or ``start()`` and ``stop()``."""

from __future__ import annotations

import base64
import json
import threading
import urllib.parse
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Self

FIXTURES = Path(__file__).parent / "fixtures"
CANARY = "CANARY-TEXT-NEVER-LEAVES"      # in every title, summary and message text the fakes serve


@dataclass
class Request:
    method: str
    path: str
    query: dict[str, list[str]] = field(default_factory=dict)
    body: Any = None
    authorized: bool = False


def load_fixture(fixture: str | Path | dict | None, default: str) -> dict:
    if isinstance(fixture, dict):
        return json.loads(json.dumps(fixture))            # a copy the fake may change
    path = Path(fixture) if fixture else FIXTURES / default
    return json.loads(path.read_text(encoding="utf-8"))


class FakeServer:
    fixture_name = ""
    auth_format = "Bearer {token}"

    def __init__(self, fixture: str | Path | dict | None = None, token: str | None = None,
                 basic_user: str | None = None):
        self.data = load_fixture(fixture, self.fixture_name)
        self.token, self.basic_user = token, basic_user
        self.requests: list[Request] = []
        self._fail: list[tuple[int, dict[str, str]]] = []
        self._lock = threading.Lock()
        self._server: ThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None

    # what a subclass implements
    def route(self, method: str, path: str, query: dict[str, list[str]], body: Any) -> tuple[int, Any]:
        raise NotImplementedError

    def response_headers(self, method: str, path: str) -> dict[str, str]:
        """Headers a routed answer carries (a system's rate-limit headers); none by default."""
        return {}

    def expected_auth(self) -> str:
        """The ``Authorization`` a request must carry: ``auth_format`` with the token (``{basic}``:
        base64 of ``basic_user:token``, as ``CachedClient`` sends it)."""
        pair = f"{self.basic_user}:{self.token}" if self.basic_user else str(self.token)
        return self.auth_format.format(token=self.token, basic=base64.b64encode(pair.encode()).decode())

    # the test's controls
    def fail_next(self, status: int, headers: dict[str, str] | None = None, count: int = 1) -> None:
        with self._lock:
            self._fail += [(status, dict(headers or {}))] * count

    def requests_to(self, path: str) -> list[Request]:
        return [r for r in self.requests if r.path == path]

    @property
    def url(self) -> str:
        if self._server is None:
            raise RuntimeError("the fake is not started")
        return f"http://127.0.0.1:{self._server.server_address[1]}"

    def start(self) -> Self:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):          # quiet
                pass

            def _serve(self, method: str):
                u = urllib.parse.urlsplit(self.path)
                query = urllib.parse.parse_qs(u.query, keep_blank_values=True)
                n = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(n) if n else b""
                ctype = self.headers.get("Content-Type", "")
                if "x-www-form-urlencoded" in ctype:
                    body: Any = urllib.parse.parse_qs(raw.decode(), keep_blank_values=True)
                    for k, v in body.items():           # a form's fields count as query parameters
                        query.setdefault(k, []).extend(v)
                else:
                    try:
                        body = json.loads(raw) if raw else None
                    except ValueError:
                        body = raw.decode("utf-8", "replace")
                expected = fake.expected_auth() if fake.token else None
                authorized = expected is not None and self.headers.get("Authorization") == expected
                with fake._lock:
                    fake.requests.append(Request(method, u.path, query, body, authorized))
                    fail = fake._fail.pop(0) if fake._fail else None
                if fail:
                    status, headers, out = fail[0], fail[1], {"error": "injected"}
                elif fake.token and not authorized:
                    status, headers, out = 401, {}, {"error": "unauthorized"}
                else:
                    try:
                        with fake._lock:
                            status, out = fake.route(method, u.path, query, body)
                            headers = fake.response_headers(method, u.path)
                    except Exception as e:  # noqa: BLE001 - a fake answers 500, as a server would
                        status, out, headers = 500, {"error": type(e).__name__}, {}
                payload = b"" if out is None else json.dumps(out).encode()
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                if payload:
                    self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def do_GET(self):
                self._serve("GET")

            def do_POST(self):
                self._serve("POST")

            def do_PUT(self):
                self._serve("PUT")

            def do_DELETE(self):
                self._serve("DELETE")

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._server.daemon_threads = True
        self._thread = threading.Thread(target=self._server.serve_forever, args=(0.05,), daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        if self._server is not None:
            self._server.shutdown()
            self._server.server_close()
            self._server = None

    def __enter__(self) -> Self:
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()


def one(query: dict[str, list[str]], name: str, default: str | None = None) -> str | None:
    v = query.get(name)
    return v[-1] if v else default
