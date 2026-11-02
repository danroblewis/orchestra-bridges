"""A fake of Slack's Web API, in its shapes, for what a poller reads (paths under ``/api/``):

- ``conversations.history`` (``channel``, ``oldest``/``latest`` exclusive unless ``inclusive``,
  ``limit`` and ``cursor`` paging with ``has_more`` and ``response_metadata.next_cursor``; newest
  first): **the channel's top-level messages only**, as Slack answers (a reply is not in the
  history unless it was also sent to the channel, ``subtype: thread_broadcast``); a thread's parent
  carries ``reply_count``, ``reply_users``, ``latest_reply`` and its ``reactions`` (``name``,
  ``users``, ``count``). ``replies_in_history=True`` is the older fake's shape, replies in the
  history too, for a test written against it;
- ``conversations.replies`` (``channel``, ``ts``, ``oldest``/``latest``, ``inclusive``, ``limit``,
  ``cursor``): the parent first, then its replies, oldest first;
- ``conversations.info`` (``channel``): its id, name and whether it is private;
- ``users.info`` (``user``).

Each answers HTTP 200 with ``ok: false`` and an ``error`` for a bad argument, as Slack does.
Every message text and real name carries the canary. ``post`` adds a message (a reply with
``thread_ts``), ``react`` and ``unreact`` a reaction on one, between polls; ``play`` schedules a
scripted day (``slack_day.Day``) that each request first brings up to the time (``advance``)."""

from __future__ import annotations

import time
from typing import Any

from .base import CANARY, FakeServer, one


class FakeSlack(FakeServer):
    fixture_name = "slack.json"

    def __init__(self, *a, replies_in_history: bool = False, **kw):
        super().__init__(*a, **kw)
        self.replies_in_history = replies_in_history
        self._script: list[tuple[float, dict]] = []     # (when, action), in order
        self._threads: dict[tuple[str, int], str] = {}   # a scripted thread's (channel, n) -> its ts
        self._auto = True

    # the test's controls
    def post(self, channel: str, user: str, ts: str | None = None, thread_ts: str | None = None,
             mentions: list[str] | None = None, bot: bool = False) -> str:
        """A message by ``user`` (a reply when ``thread_ts``; a bot's, with ``bot_id``, when
        ``bot``), mentioning ``mentions`` (member ids, as ``<@U001>`` in its text); its ts."""
        with self._lock:
            return self._post(channel, user, ts, thread_ts, mentions, bot)

    def _post(self, channel, user, ts=None, thread_ts=None, mentions=None, bot=False) -> str:
        ts = ts or f"{time.time():.6f}"
        text = " ".join([CANARY, *(f"<@{u}>" for u in mentions or []), "message"])
        m = {"ts": ts, "user": user, "text": text}
        if bot:
            m |= {"bot_id": user, "subtype": "bot_message"}
        if thread_ts:
            m["thread_ts"] = thread_ts
        self.data["channels"].setdefault(channel, []).append(m)
        return ts

    def react(self, channel: str, ts: str, user: str, name: str = "white_check_mark") -> None:
        with self._lock:
            self._react(channel, ts, user, name, True)

    def unreact(self, channel: str, ts: str, user: str, name: str = "white_check_mark") -> None:
        with self._lock:
            self._react(channel, ts, user, name, False)

    def _react(self, channel, ts, user, name, on: bool) -> None:
        m = self._message(channel, ts)
        if m is None:
            raise KeyError(f"{channel}/{ts}: no such message")
        users = m.setdefault("_reactions", {}).setdefault(name, [])
        if on and user not in users:
            users.append(user)
        elif not on and user in users:
            users.remove(user)

    def play(self, day, t0: float, scale: float = 1.0, auto: bool = True) -> None:
        """Schedule ``day``'s actions (``slack_day.Day``): each at ``t0 + at * scale`` (a day of
        86,400 s played in ten minutes is ``scale = 600 / 86400``). With ``auto`` each request first
        brings it up to the time; else only ``advance`` does (a test stepping through the day)."""
        with self._lock:
            self._auto = auto
            self._script = sorted(((t0 + a["at"] * scale, a) for a in day.actions), key=lambda x: x[0])

    def advance(self, until: float | None = None) -> int:
        """Apply the scripted actions due by ``until`` (now); how many were."""
        with self._lock:
            return self._advance(time.time() if until is None else until)

    def _advance(self, until: float) -> int:
        n = 0
        while self._script and self._script[0][0] <= until:
            when, a = self._script.pop(0)
            ts = f"{when:.6f}"
            key = (a["channel"], a["thread"])
            if a["op"] == "start":
                self._threads[key] = self._post(a["channel"], a["user"], ts, bot=a.get("bot", False))
            elif a["op"] == "reply":
                self._post(a["channel"], a["user"], ts, thread_ts=self._threads[key])
            else:
                self._react(a["channel"], self._threads[key], a["user"], a["emoji"], a["op"] == "react")
            n += 1
        return n

    def _message(self, channel: str, ts: str) -> dict | None:
        return next((m for m in self.data["channels"].get(channel, []) if m["ts"] == ts), None)

    def route(self, method: str, path: str, query: dict[str, list[str]], body: Any) -> tuple[int, Any]:
        if isinstance(body, dict) and not any(isinstance(v, list) for v in body.values()):   # a JSON POST's
            query = {**query, **{k: [str(v)] for k, v in body.items()}}
        if self._script and self._auto:
            self._advance(time.time())
        if path == "/api/conversations.history":
            return 200, self._history(query)
        if path == "/api/conversations.replies":
            return 200, self._replies(query)
        if path == "/api/conversations.info":
            ch = one(query, "channel")
            if ch not in self.data["channels"]:
                return 200, {"ok": False, "error": "channel_not_found"}
            name = (self.data.get("names") or {}).get(ch, ch.lower())
            return 200, {"ok": True, "channel": {"id": ch, "name": name, "is_channel": True,
                                                 "is_private": ch in (self.data.get("private") or []),
                                                 "purpose": {"value": f"{CANARY} purpose"}}}
        if path == "/api/users.info":
            uid = one(query, "user")
            u = next((x for x in self.data["users"] if x["id"] == uid), None)
            if u is None:
                return 200, {"ok": False, "error": "user_not_found"}
            return 200, {"ok": True, "user": {"id": u["id"], "name": u["name"], "real_name": u["real_name"],
                                               "is_bot": u["is_bot"], "deleted": False, "tz": u["tz"],
                                               "profile": {"real_name": u["real_name"], "display_name": u["name"]}}}
        return 404, {"ok": False, "error": "unknown_method"}

    def _window(self, q: dict[str, list[str]]):
        oldest = float(one(q, "oldest", "0") or 0)
        latest = float(one(q, "latest", "0") or 0) or float("inf")
        limit = max(1, min(int(one(q, "limit", "100")), 999))
        start = int(one(q, "cursor", "0") or 0)
        inclusive = one(q, "inclusive") in ("true", "1")
        return (lambda t: oldest <= t <= latest if inclusive else oldest < t < latest), limit, start

    @staticmethod
    def _top(m: dict) -> bool:
        return m.get("thread_ts") in (None, m["ts"]) or m.get("subtype") == "thread_broadcast"

    def _shown(self, m: dict, ch: str) -> dict:
        """A message as Slack shows it: a parent with its thread's summary and reactions."""
        out = {"type": "message", **{k: v for k, v in m.items() if not k.startswith("_")}}
        replies = [r for r in self.data["channels"][ch] if r.get("thread_ts") == m["ts"] and r["ts"] != m["ts"]]
        if replies:
            out |= {"thread_ts": m["ts"], "reply_count": len(replies),
                    "reply_users": list(dict.fromkeys(r["user"] for r in replies)),
                    "reply_users_count": len({r["user"] for r in replies}),
                    "latest_reply": max(replies, key=lambda r: float(r["ts"]))["ts"]}
        reactions = [{"name": n, "users": list(u), "count": len(u)} for n, u in (m.get("_reactions") or {}).items() if u]
        if reactions:
            out["reactions"] = reactions
        return out

    def _page(self, msgs: list[dict], ch: str, limit: int, start: int, newest_first: bool) -> dict:
        msgs.sort(key=lambda m: float(m["ts"]), reverse=newest_first)
        page = msgs[start:start + limit]
        more = start + limit < len(msgs)
        return {"ok": True, "messages": [self._shown(m, ch) for m in page], "has_more": more, "pin_count": 0,
                "response_metadata": {"next_cursor": str(start + limit) if more else ""}}

    def _history(self, q: dict[str, list[str]]) -> dict:
        ch = one(q, "channel")
        if not ch:
            return {"ok": False, "error": "invalid_arguments"}
        if ch not in self.data["channels"]:
            return {"ok": False, "error": "channel_not_found"}
        try:
            within, limit, start = self._window(q)
        except ValueError:
            return {"ok": False, "error": "invalid_arguments"}
        msgs = [m for m in self.data["channels"][ch]
                if within(float(m["ts"])) and (self.replies_in_history or self._top(m))]
        return self._page(msgs, ch, limit, start, newest_first=True)

    def _replies(self, q: dict[str, list[str]]) -> dict:
        ch, ts = one(q, "channel"), one(q, "ts")
        if not ch or not ts:
            return {"ok": False, "error": "invalid_arguments"}
        if ch not in self.data["channels"]:
            return {"ok": False, "error": "channel_not_found"}
        parent = self._message(ch, ts)
        if parent is None:
            return {"ok": False, "error": "thread_not_found"}
        try:
            within, limit, start = self._window(q)
        except ValueError:
            return {"ok": False, "error": "invalid_arguments"}
        replies = [m for m in self.data["channels"][ch]
                   if m.get("thread_ts") == ts and m["ts"] != ts and within(float(m["ts"]))]
        body = self._page(replies, ch, limit, start, newest_first=False)
        if start == 0:
            body["messages"].insert(0, self._shown(parent, ch))
        return body
