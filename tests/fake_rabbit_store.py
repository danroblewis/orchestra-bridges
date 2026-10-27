"""A stdlib fake of a Prometheus-compatible store holding RabbitMQ's ``/metrics/detailed`` series
(``rabbitmq_detailed_*``), answering exactly what the RabbitMQ preset asks of it.

Raw reads (an instant ``query`` of ``selector[range]``) are Orchestra's PromQL subset. The preset's
range queries use what that subset lacks (``label_replace``, ``* on (…) group_left (…)``), so the
fake evaluates those two shapes itself, with Prometheus' semantics: the last sample within five
minutes at each step, a one-to-one match on ``vhost, queue`` taking ``exchange`` from the right,
and a fully anchored regex. A query of any other shape is answered 400, so a change to the
preset's queries shows here before it reaches a real store (the live test checks the real one)."""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.parse
from bisect import bisect_right
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from orchestra.metrics import promql

RAW = re.compile(r"^(?P<sel>.*)\[(?P<n>\d+)(?P<u>[smhd])\]\s*$", re.DOTALL)
UNITS = {"s": 1, "m": 60, "h": 3600, "d": 86400}
LOOKBACK = 300.0
JOIN = re.compile(r"^label_replace\(sum by \(vhost, queue, exchange\) \((?P<a>\w+)\s+\* on \(vhost, queue\) group_left \(exchange\) "
                  r"count by \(vhost, queue, exchange\) \((?P<b>\w+)\)\),\s*(?P<repl>.*)\)$", re.DOTALL)
SUMQ = re.compile(r"^label_replace\(sum by \(vhost, queue\) \((?P<a>\w+)\),\s*(?P<repl>.*)\)$", re.DOTALL)
REPL = re.compile(r'^"(?P<dst>\w+)", "\$1", "(?P<src>\w+)", "(?P<re>[^"]+)"$')


class FakeRabbitStore:
    def __init__(self, dump):
        self.dump = list(dump)                    # (name, labels, [(t, v)])
        self.queries: list[str] = []
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()

    def _at(self, name: str, t: float) -> list[tuple[dict, float]]:
        out = []
        for n, lab, pts in self.dump:
            if n != name:
                continue
            i = bisect_right([p[0] for p in pts], t)
            if i and t - pts[i - 1][0] <= LOOKBACK:
                out.append((lab, pts[i - 1][1]))
        return out

    @staticmethod
    def _replace(labels: dict, repl: str) -> dict:
        m = REPL.match(repl.strip())
        if not m:
            raise ValueError(f"label_replace arguments not understood: {repl}")
        got = re.fullmatch(m["re"], labels.get(m["src"], ""))
        return {**labels, m["dst"]: got.group(1)} if got else labels

    def evaluate(self, q: str, t: float) -> dict[tuple, tuple[dict, float]]:
        out: dict[tuple, tuple[dict, float]] = {}
        if m := JOIN.match(q.strip()):
            right = {}
            for lab, _ in self._at(m["b"], t):
                right.setdefault((lab["vhost"], lab["queue"]), set()).add(lab["exchange"])
            for lab, v in self._at(m["a"], t):
                exchanges = right.get((lab["vhost"], lab["queue"]), set())
                if len(exchanges) > 1:
                    raise ValueError("many-to-many matching not allowed: a queue bound to two exchanges")
                for ex in exchanges:
                    key = {"vhost": lab["vhost"], "queue": lab["queue"], "exchange": ex}
                    k = tuple(sorted(key.items()))
                    out[k] = (key, out.get(k, (key, 0.0))[1] + v)
            repl = m["repl"]
        elif m := SUMQ.match(q.strip()):
            for lab, v in self._at(m["a"], t):
                key = {"vhost": lab["vhost"], "queue": lab["queue"]}
                k = tuple(sorted(key.items()))
                out[k] = (key, out.get(k, (key, 0.0))[1] + v)
            repl = m["repl"]
        else:
            raise ValueError(f"the fake does not evaluate {q!r}")
        return {k: (self._replace(lab, repl), v) for k, (lab, v) in out.items()}

    def query_range(self, q: str, start: float, end: float, step: float) -> list[dict]:
        series: dict[tuple, dict] = {}
        t = start
        while t <= end + 1e-9:
            for k, (lab, v) in self.evaluate(q, t).items():
                series.setdefault(k, {"metric": lab, "values": []})["values"].append([t, repr(float(v))])
            t += step
        return list(series.values())

    def answer(self, endpoint: str, form: dict) -> dict:
        def one(k, d=None):
            return (form.get(k) or [d])[0]

        q = one("query")
        self.queries.append(q)
        if endpoint == "query_range":
            return {"resultType": "matrix", "result": self.query_range(q, float(one("start")), float(one("end")), float(one("step")))}
        m = RAW.match(q or "")
        if endpoint == "query" and m:
            at, d = float(one("time", time.time())), int(m["n"]) * UNITS[m["u"]]
            got = promql.select(self.dump, m["sel"], at - d, at)
            return {"resultType": "matrix", "result": [{"metric": lab, "values": [[t, repr(float(v))] for t, v in pts if t > at - d]}
                                                       for lab, pts in got]}
        raise ValueError(f"the fake does not answer {endpoint} {q!r}")

    def _handler(self):
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                form = urllib.parse.parse_qs(self.rfile.read(n).decode())
                endpoint = urllib.parse.urlsplit(self.path).path.rsplit("/", 1)[-1]
                try:
                    body, code = {"status": "success", "data": fake.answer(endpoint, form)}, 200
                except (ValueError, promql.PromQLError) as e:
                    body, code = {"status": "error", "errorType": "bad_data", "error": str(e)}, 400
                raw = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        return H
