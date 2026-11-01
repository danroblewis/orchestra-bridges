"""A fake of Slack's Web API, in its shapes, for what a poller reads: ``conversations.history``
(``channel``, ``oldest``/``latest`` exclusive, ``inclusive``, ``limit`` and ``cursor`` paging with
``has_more`` and ``response_metadata.next_cursor``; newest first) and ``users.info`` (``user``).
Both answer HTTP 200 with ``ok: false`` and an ``error`` for a bad argument, as Slack does; a
missing token is ``not_authed``. Paths are under ``/api/``. Every message text and real name
carries the canary. ``post`` adds a message (with mentions) between polls."""

from __future__ import annotations

import time
from typing import Any

from .base import CANARY, FakeServer, one


class FakeSlack(FakeServer):
    fixture_name = "slack.json"

    # the test's controls
    def post(self, channel: str, user: str, ts: str | None = None, thread_ts: str | None = None,
             mentions: list[str] | None = None) -> str:
        """A message by ``user`` (a reply when ``thread_ts``), mentioning ``mentions`` (member ids, as
        ``<@U001>`` in its text); its ts."""
        with self._lock:
            ts = ts or f"{time.time():.6f}"
            text = " ".join([CANARY, *(f"<@{u}>" for u in mentions or []), "message"])
            m = {"ts": ts, "user": user, "text": text}
            if thread_ts:
                m["thread_ts"] = thread_ts
            self.data["channels"].setdefault(channel, []).append(m)
            return ts

    def route(self, method: str, path: str, query: dict[str, list[str]], body: Any) -> tuple[int, Any]:
        if isinstance(body, dict) and not any(isinstance(v, list) for v in body.values()):   # a JSON POST's
            query = {**query, **{k: [str(v)] for k, v in body.items()}}
        if path == "/api/conversations.history":
            return 200, self._history(query)
        if path == "/api/users.info":
            uid = one(query, "user")
            u = next((x for x in self.data["users"] if x["id"] == uid), None)
            if u is None:
                return 200, {"ok": False, "error": "user_not_found"}
            return 200, {"ok": True, "user": {"id": u["id"], "name": u["name"], "real_name": u["real_name"],
                                               "is_bot": u["is_bot"], "deleted": False, "tz": u["tz"],
                                               "profile": {"real_name": u["real_name"], "display_name": u["name"]}}}
        return 404, {"ok": False, "error": "unknown_method"}

    def _history(self, q: dict[str, list[str]]) -> dict:
        ch = one(q, "channel")
        if not ch:
            return {"ok": False, "error": "invalid_arguments"}
        if ch not in self.data["channels"]:
            return {"ok": False, "error": "channel_not_found"}
        try:
            oldest = float(one(q, "oldest", "0") or 0)
            latest = float(one(q, "latest", "0") or 0) or float("inf")
            limit = max(1, min(int(one(q, "limit", "100")), 999))
            start = int(one(q, "cursor", "0") or 0)
        except ValueError:
            return {"ok": False, "error": "invalid_arguments"}
        inclusive = one(q, "inclusive") in ("true", "1")
        msgs = [m for m in self.data["channels"][ch]
                if (oldest <= float(m["ts"]) <= latest if inclusive else oldest < float(m["ts"]) < latest)]
        msgs.sort(key=lambda m: float(m["ts"]), reverse=True)
        page = msgs[start:start + limit]
        more = start + limit < len(msgs)
        out = [{"type": "message", **m} for m in page]
        return {"ok": True, "messages": out, "has_more": more, "pin_count": 0,
                "response_metadata": {"next_cursor": str(start + limit) if more else ""}}
