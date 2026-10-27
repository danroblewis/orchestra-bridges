"""A fake of Slack's Web API, speaking the real shapes, for the three methods the bridge calls.

It serves ``auth.test``, ``search.messages`` (paged by ``page`` and ``count``, newest first with
``sort=timestamp``) and ``reactions.get``, under ``/api/``. It demands the bearer token (a wrong one
is ``ok: false, invalid_auth``, as Slack answers), and it can be told to answer the next requests
with a rate limit (429 and ``Retry-After``) or to be **asleep** (every connection dropped, as for a
laptop whose network is gone).

The world is scripted ahead of time against a clock: a message, a reply or a reaction exists only
once the clock has reached its time, so a test lays out a morning and moves the clock through it.
Every message's text carries ``CANARY``, which must never leave the bridge.

Search understands what the bridge asks: ``from:<@U>``, ``to:<@U>`` (a direct message to ``U``),
``after:YYYY-MM-DD`` (strictly after that day, UTC), and any other word as text the message must
contain (``<@U...>``, ``<!subteam^S...>``). Replies carry their thread only in the permalink
(``?thread_ts=``), the harder of the two ways Slack shows it."""

from __future__ import annotations

import itertools
import json
import threading
import time
import urllib.parse
from collections.abc import Callable
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Self

CANARY = "CANARY-5c2e-never-publish-me"
TOKEN = "fake-slack-user-token-for-tests"
TEAM = "T0FAKE"
OWNER, OWNER_NAME = "U0OWNER", "daniel"
GROUP = "S0SRE"
CHANNEL, OTHER = "C0GENERAL", "C0RANDOM"


def channel(cid: str, kind: str = "channel", name: str | None = None) -> dict:
    """A search match's channel object: ``kind`` is channel, group (private), im or mpim."""
    return {"id": cid, "is_channel": kind == "channel", "is_group": kind == "group", "is_im": kind == "im",
            "is_mpim": kind == "mpim", "is_shared": False, "is_org_shared": False, "is_ext_shared": False,
            "is_private": kind in ("group", "im", "mpim"), "is_pending_ext_shared": False, "pending_shared": [],
            "name": name or cid.lower()}


class FakeSlack:
    def __init__(self, clock: Callable[[], float] = time.time, per_page_max: int = 100):
        self.clock, self.per_page_max = clock, per_page_max
        self.users: dict[str, str] = {OWNER: OWNER_NAME}         # member id -> handle
        self.channels: dict[str, dict] = {CHANNEL: channel(CHANNEL, name="general"),
                                          OTHER: channel(OTHER, name="random")}
        self.dm_with: dict[str, str] = {}                        # im channel -> the owner's counterpart
        self.messages: list[dict] = []
        self.reacts: dict[tuple[str, str], list[tuple[str, str, float]]] = {}   # (channel, ts) -> (name, user, at)
        self.deleted: set[tuple[str, str]] = set()
        self.requests: list[tuple[str, dict, str | None]] = []
        self.fail_next: list[tuple[int, dict]] = []             # (status, headers) for the next requests
        self.asleep = False
        self._seq = itertools.count(1)
        self._server: ThreadingHTTPServer | None = None
        self.base = ""

    def user(self, uid: str, handle: str) -> str:
        self.users[uid] = handle
        return uid

    def dm(self, uid: str) -> str:
        """The direct-message channel between ``uid`` and the owner."""
        cid = f"D0{uid.removeprefix('U')}"
        self.channels[cid] = channel(cid, "im", name=OWNER)
        self.dm_with[cid] = uid
        return cid

    def say(self, uid: str, cid: str, at: float, *, mentions: tuple[str, ...] = (), thread: str | None = None,
            bot: bool = False) -> str:
        """``uid`` posts in ``cid`` at ``at`` (a reply in ``thread`` when given), mentioning ``mentions``
        (member or group ids); its ts."""
        ts = f"{int(at)}.{next(self._seq):06d}"
        tags = " ".join(f"<!subteam^{m}|@sre>" if m.startswith("S") else f"<@{m}>" for m in mentions)
        self.messages.append({"channel": cid, "ts": ts, "user": uid, "thread": thread, "bot": bot,
                              "text": f"{tags} {CANARY} please look".strip()})
        return ts

    def react(self, cid: str, ts: str, name: str, uid: str, at: float) -> None:
        self.reacts.setdefault((cid, ts), []).append((name, uid, at))

    def delete(self, cid: str, ts: str) -> None:
        self.deleted.add((cid, ts))

    def _visible(self) -> list[dict]:
        now = self.clock()
        return [m for m in self.messages if float(m["ts"]) <= now and (m["channel"], m["ts"]) not in self.deleted]

    def _matches(self, query: str, m: dict) -> bool:
        for word in query.split():
            if word.startswith("from:"):
                ok = m["user"] == word.removeprefix("from:<@").rstrip(">")
            elif word.startswith("to:"):
                to = word.removeprefix("to:<@").rstrip(">")
                ok = m["channel"] in self.dm_with and m["user"] != to and to == OWNER
            elif word.startswith("after:"):
                ok = datetime.fromtimestamp(float(m["ts"]), UTC).strftime("%Y-%m-%d") > word.removeprefix("after:")
            elif word.startswith("<") and word.endswith(">"):     # a mention, with or without its |label
                ok = word in m["text"] or f"{word[:-1]}|" in m["text"]
            else:
                ok = word in m["text"]
            if not ok:
                return False
        return True

    def match(self, m: dict) -> dict:
        cid, ts = m["channel"], m["ts"]
        link = f"https://fake.slack.com/archives/{cid}/p{ts.replace('.', '')}"
        if m["thread"]:
            link += f"?thread_ts={m['thread']}&cid={cid}"
        out = {"iid": f"{cid}-{ts}", "team": TEAM, "score": 0, "channel": self.channels[cid], "type": "message",
               "user": m["user"], "username": self.users.get(m["user"], m["user"].lower()), "ts": ts,
               "blocks": [{"type": "rich_text", "block_id": "b1",
                           "elements": [{"type": "rich_text_section", "elements": [{"type": "text", "text": m["text"]}]}]}],
               "text": m["text"], "permalink": link}
        if m["bot"]:
            out["bot_id"] = f"B{m['user']}"
        return out

    def search(self, q: dict) -> dict:
        query = q.get("query", "")
        found = sorted((m for m in self._visible() if self._matches(query, m)), key=lambda m: float(m["ts"]),
                       reverse=q.get("sort_dir", "desc") == "desc")
        per = min(int(q.get("count", 20)), self.per_page_max)
        page = int(q.get("page", 1))
        pages = max((len(found) + per - 1) // per, 1)
        chunk = found[(page - 1) * per: page * per]
        return {"ok": True, "query": query, "messages": {
            "total": len(found),
            "pagination": {"total_count": len(found), "page": page, "per_page": per, "page_count": pages,
                           "first": (page - 1) * per + 1, "last": (page - 1) * per + len(chunk)},
            "paging": {"count": per, "total": len(found), "page": page, "pages": pages},
            "matches": [self.match(m) for m in chunk]}}

    def reactions_get(self, q: dict) -> dict:
        key = (q.get("channel"), q.get("timestamp"))
        [m] = [m for m in self._visible() if (m["channel"], m["ts"]) == key] or [None]
        if m is None:
            return {"ok": False, "error": "message_not_found"}
        now = self.clock()
        by: dict[str, list[str]] = {}
        for name, uid, at in self.reacts.get(key, []):
            if at <= now:
                by.setdefault(name, []).append(uid)
        msg = {"type": "message", "text": m["text"], "user": m["user"], "ts": m["ts"], "team": TEAM,
               "permalink": self.match(m)["permalink"]}
        if by:
            msg["reactions"] = [{"name": n, "users": us, "count": len(us)} for n, us in by.items()]
        return {"ok": True, "type": "message", "channel": key[0], "message": msg}

    def start(self) -> Self:
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):  # quiet
                pass

            def _send(self, status: int, body: dict, headers: dict | None = None):
                raw = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                u = urllib.parse.urlsplit(self.path)
                q = dict(urllib.parse.parse_qsl(u.query))
                auth = self.headers.get("Authorization")
                method = u.path.removeprefix("/api/")
                fake.requests.append((method, q, auth))
                if fake.asleep:
                    self.close_connection = True
                    self.connection.close()
                    return None
                if fake.fail_next:
                    status, headers = fake.fail_next.pop(0)
                    return self._send(status, {"ok": False, "error": "ratelimited" if status == 429 else "fatal_error"},
                                      headers)
                if auth != f"Bearer {TOKEN}":
                    return self._send(200, {"ok": False, "error": "invalid_auth"})
                if method == "auth.test":
                    return self._send(200, {"ok": True, "url": "https://fake.slack.com/", "team": "Fake", "user": OWNER_NAME,
                                            "team_id": TEAM, "user_id": OWNER, "is_enterprise_install": False})
                if method == "search.messages":
                    return self._send(200, fake.search(q))
                if method == "reactions.get":
                    return self._send(200, fake.reactions_get(q))
                return self._send(404, {"ok": False, "error": "unknown_method"})

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{self._server.server_address[1]}/api"
        threading.Thread(target=self._server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        return self

    def stop(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server.server_close()

    def __enter__(self) -> Self:
        return self.start()

    def __exit__(self, *exc) -> None:
        self.stop()
