"""``/metrics``: the bridge's counts in the Prometheus text format, for the instrument that stores
them (the scrape ``bridges.slack`` the plugin declares, ``plugin.py``).

It listens on ``127.0.0.1`` by default, answers ``GET /metrics`` with what ``render`` returns (or
503 while that is None: the bridge has not read its past back yet) and anything else with 404. It
logs no requests."""

from __future__ import annotations

import threading
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class MetricsServer:
    def __init__(self, render: Callable[[], str | None], host: str = "127.0.0.1", port: int = 9465):
        self.render = render
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # quiet: the log holds the bridge's own lines only
                pass

            def do_GET(self):
                if self.path.split("?")[0] != "/metrics":
                    return self._send(404, "not found\n")
                body = server.render()
                if body is None:
                    return self._send(503, "not ready\n")
                return self._send(200, body)

            def _send(self, status: int, text: str):
                raw = text.encode()
                self.send_response(status)
                self.send_header("Content-Type", "text/plain; version=0.0.4; charset=utf-8")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        self._httpd = ThreadingHTTPServer((host, port), Handler)
        self.host, self.port = host, self._httpd.server_address[1]
        self._thread = threading.Thread(target=self._httpd.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True)

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}/metrics"

    def start(self) -> MetricsServer:
        self._thread.start()
        return self

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
