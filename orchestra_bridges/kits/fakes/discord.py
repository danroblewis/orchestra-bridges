"""A fake of Discord's REST API (v10, paths under ``/api/v10``), in its shapes, for what a poller
reads with a bot token (``Authorization: Bot <token>``):

- ``GET /guilds/{guild}/channels``: the guild's channels (id, type, name, ``guild_id``, a topic);
- ``GET /guilds/{guild}/threads/active``: the active threads (``threads``, ``members``);
- ``GET /channels/{id}``: a channel, or a thread (type 11, ``parent_id``, ``message_count``,
  ``last_message_id``, ``thread_metadata``);
- ``GET /channels/{id}/messages`` (``after``, ``before``, ``limit`` 1 to 100, default 50; newest
  first, as Discord answers): a channel's messages (``type`` 0, or 19 for an inline reply with
  ``message_reference`` and ``referenced_message``), each with its ``reactions`` (``emoji``,
  ``count``: never who) and, when a thread was started from it, its ``thread``; a thread's
  messages (its starter, type 21, first);
- ``GET /channels/{id}/threads/archived/public`` (``before``, ``limit``): the archived threads;
- ``GET /channels/{id}/messages/{message}/reactions/{emoji}`` (``limit``, ``after``): who reacted,
  by user id.

An unknown channel or message answers 404 ``{"message": "Unknown Channel", "code": 10003}`` (or
10008), as Discord does. Every answer carries Discord's rate-limit headers (``X-RateLimit-Limit``,
``-Remaining``, ``-Reset-After``, ``-Bucket``: ``rate``, which a test may change), and
``fail_next(429, {"Retry-After": "1"})`` answers a rate limit. Every message text, thread name,
topic and person's name carries the canary.

The world (``data``): ``guild`` (``id``, ``name``), ``users`` (``id``, ``name``, ``real_name``,
``bot`` or ``is_bot``), ``names`` (channel id to name), ``channels`` (channel id to its messages:
``at`` in Unix seconds or an ``id``, ``user``, ``key``, ``reply_to`` a key, ``thread_of`` a key),
``other_channels`` (a voice channel, a category: listed, never read). ``slack_day.Day.world()`` is
one too: ``play`` plays its script (a start is a message, a reply one in the thread started from
it, a reaction ``white_check_mark`` the ✅). ``post``, ``reply`` (in the thread), ``react``,
``unreact`` and ``archive`` change it between polls."""

from __future__ import annotations

import time
import urllib.parse
from datetime import UTC, datetime
from typing import Any

from .base import CANARY, FakeServer, one

API = "/api/v10"
EPOCH_MS = 1420070400000                  # Discord's epoch, 2015-01-01, in a snowflake's milliseconds
EMOJI = {"white_check_mark": "✅", "heavy_check_mark": "✔️", "ballot_box_with_check": "☑️"}
TEXT, VOICE, CATEGORY, ANNOUNCEMENT, THREAD, FORUM = 0, 2, 4, 5, 11, 15


def snowflake(t: float) -> int:
    return max(0, int(t * 1000) - EPOCH_MS) << 22


def time_of(sid: str | int) -> float:
    return ((int(sid) >> 22) + EPOCH_MS) / 1000.0


def iso(sid: str | int) -> str:
    return datetime.fromtimestamp(time_of(sid), UTC).isoformat()


class FakeDiscord(FakeServer):
    fixture_name = "discord.json"
    auth_format = "Bot {token}"

    def __init__(self, *a, **kw):
        super().__init__(*a, **kw)
        self.rate = {"X-RateLimit-Limit": "50", "X-RateLimit-Remaining": "49", "X-RateLimit-Reset-After": "1.000",
                     "X-RateLimit-Bucket": "fakebucket"}
        self._used: set[int] = set()
        self._script: list[tuple[float, dict]] = []
        self._started: dict[tuple[str, int], str] = {}      # a scripted thread's (channel, n) -> its parent's id
        self._auto = True
        d = self.data
        d.setdefault("guild", {"id": "1000", "name": "fake"})
        d.setdefault("names", {})
        d.setdefault("threads", {})
        d.setdefault("channels", {})
        d["users"] = [{**u, "bot": bool(u.get("bot", u.get("is_bot", False)))} for u in d.get("users") or []]
        given, keys = d["channels"], {}
        d["channels"] = {ch: [] for ch in given}
        for ch, msgs in given.items():
            for m in sorted(msgs, key=lambda m: float(m.get("at") or time_of(m["id"]))):
                at = float(m["at"]) if "at" in m else None
                if "thread_of" in m:
                    mid = self._reply(ch, keys[m["thread_of"]], m["user"], at, m.get("bot", False), m.get("id"))
                else:
                    mid = self._post(ch, m["user"], at, m.get("bot", False), keys.get(m.get("reply_to")), m.get("id"))
                keys[m.get("key") or mid] = mid

    # the test's controls
    def post(self, channel: str, user: str, at: float | None = None, bot: bool = False,
             reply_to: str | None = None) -> str:
        """A message by ``user`` in the channel (an inline reply to ``reply_to``); its id."""
        with self._lock:
            return self._post(channel, user, at, bot, reply_to)

    def reply(self, channel: str, parent: str, user: str, at: float | None = None, bot: bool = False) -> str:
        """A message in the thread started from ``parent`` (started now, if it was not); its id."""
        with self._lock:
            return self._reply(channel, parent, user, at, bot)

    def react(self, channel: str, mid: str, user: str, emoji: str = "✅") -> None:
        with self._lock:
            self._react(channel, mid, user, emoji, True)

    def unreact(self, channel: str, mid: str, user: str, emoji: str = "✅") -> None:
        with self._lock:
            self._react(channel, mid, user, emoji, False)

    def archive(self, parent: str, archived: bool = True) -> None:
        with self._lock:
            self.data["threads"][parent]["archived"] = archived

    def play(self, day, t0: float, scale: float = 1.0, auto: bool = True) -> None:
        """Schedule ``day``'s actions (``slack_day.Day``), each at ``t0 + at * scale``; with ``auto``
        each request first brings it up to the time, else only ``advance`` does."""
        with self._lock:
            self._auto = auto
            self._script = sorted(((t0 + a["at"] * scale, a) for a in day.actions), key=lambda x: x[0])

    def advance(self, until: float | None = None) -> int:
        with self._lock:
            return self._advance(time.time() if until is None else until)

    def _advance(self, until: float) -> int:
        n = 0
        while self._script and self._script[0][0] <= until:
            when, a = self._script.pop(0)
            key = (a["channel"], a["thread"])
            if a["op"] == "start":
                self._started[key] = self._post(a["channel"], a["user"], when, a.get("bot", False))
            elif a["op"] == "reply":
                self._reply(a["channel"], self._started[key], a["user"], when)
            else:
                emoji = EMOJI.get(a["emoji"], a["emoji"])
                self._react(a["channel"], self._started[key], a["user"], emoji, a["op"] == "react")
            n += 1
        return n

    def _id(self, at: float | None) -> str:
        n = snowflake(time.time() if at is None else at)
        while n in self._used:
            n += 1
        self._used.add(n)
        return str(n)

    def _post(self, channel, user, at=None, bot=False, reply_to=None, mid=None) -> str:
        mid = mid or self._id(at)
        m = {"id": mid, "user": user, "bot": bot or self._is_bot(user), "type": 19 if reply_to else 0,
             "content": f"{CANARY} message"}
        if reply_to:
            m["ref"] = reply_to
        self.data["channels"].setdefault(channel, []).append(m)
        return mid

    def _reply(self, channel, parent, user, at=None, bot=False, mid=None) -> str:
        if self._message(channel, parent) is None:
            raise KeyError(f"{channel}/{parent}: no such message")
        t = self.data["threads"].setdefault(parent, {"channel": channel, "archived": False, "messages": [
            {"id": parent, "user": self._message(channel, parent)["user"], "bot": False, "type": 21,
             "ref": parent, "content": ""}]})
        mid = mid or self._id(at)
        t["messages"].append({"id": mid, "user": user, "bot": bot or self._is_bot(user), "type": 0,
                              "content": f"{CANARY} reply"})
        return mid

    def _react(self, channel, mid, user, emoji, on: bool) -> None:
        m = self._message(channel, mid)
        if m is None:
            raise KeyError(f"{channel}/{mid}: no such message")
        users = m.setdefault("_reactions", {}).setdefault(emoji, [])
        if on and user not in users:
            users.append(user)
        elif not on and user in users:
            users.remove(user)

    def _message(self, channel: str, mid: str) -> dict | None:
        return next((m for m in self.data["channels"].get(channel, []) if m["id"] == mid), None)

    def _is_bot(self, uid: str) -> bool:
        return any(u["id"] == uid and u["bot"] for u in self.data["users"])

    def _user(self, uid: str, bot: bool = False) -> dict:
        u = next((x for x in self.data["users"] if x["id"] == uid), None)
        name = u["name"] if u else uid.lower()
        out = {"id": uid, "username": name, "global_name": f"{CANARY} {name}", "discriminator": "0", "avatar": None}
        if bot or (u and u["bot"]):
            out["bot"] = True
        return out

    # the API
    def response_headers(self, method: str, path: str) -> dict[str, str]:
        return dict(self.rate)

    def route(self, method: str, path: str, query: dict[str, list[str]], body: Any) -> tuple[int, Any]:
        if self._script and self._auto:
            self._advance(time.time())
        if method != "GET":
            return 405, {"message": "405: Method Not Allowed", "code": 0}
        if not path.startswith(API + "/"):
            return 404, {"message": "404: Not Found", "code": 0}
        parts = [urllib.parse.unquote(p) for p in path[len(API) + 1:].split("/")]
        guild = self.data["guild"]["id"]
        if parts[0] == "guilds":
            if len(parts) < 3 or parts[1] != guild:
                return 404, {"message": "Unknown Guild", "code": 10004}
            if parts[2:] == ["channels"]:
                return 200, [self._channel(ch) for ch in self.data["channels"]] + list(self.data.get("other_channels") or [])
            if parts[2:] == ["threads", "active"]:
                return 200, {"threads": [self._thread_obj(p) for p, t in self.data["threads"].items() if not t["archived"]],
                             "members": []}
            return 404, {"message": "404: Not Found", "code": 0}
        if parts[0] != "channels" or len(parts) < 2:
            return 404, {"message": "404: Not Found", "code": 0}
        cid, rest = parts[1], parts[2:]
        is_thread = cid in self.data["threads"]
        if cid not in self.data["channels"] and not is_thread:
            return 404, {"message": "Unknown Channel", "code": 10003}
        if not rest:
            return 200, self._thread_obj(cid) if is_thread else self._channel(cid)
        if rest == ["messages"]:
            return self._messages(cid, query, is_thread)
        if rest == ["threads", "archived", "public"] and not is_thread:
            got = [self._thread_obj(p) for p, t in self.data["threads"].items() if t["archived"] and t["channel"] == cid]
            limit = max(2, min(int(one(query, "limit", "50")), 100))
            return 200, {"threads": got[:limit], "members": [], "has_more": len(got) > limit}
        if len(rest) == 4 and rest[0] == "messages" and rest[2] == "reactions" and not is_thread:
            m = self._message(cid, rest[1])
            if m is None:
                return 404, {"message": "Unknown Message", "code": 10008}
            emoji = rest[3].split(":")[0]
            after = int(one(query, "after", "0") or 0)
            limit = max(1, min(int(one(query, "limit", "25")), 100))
            users = sorted((u for u in (m.get("_reactions") or {}).get(emoji, []) if int_of(u) > after), key=int_of)
            return 200, [self._user(u) for u in users[:limit]]
        return 404, {"message": "404: Not Found", "code": 0}

    def _channel(self, ch: str) -> dict:
        return {"id": ch, "type": TEXT, "guild_id": self.data["guild"]["id"],
                "name": self.data["names"].get(ch, ch.lower()), "topic": f"{CANARY} topic", "position": 0,
                "last_message_id": max((m["id"] for m in self.data["channels"][ch]), key=int, default=None)}

    def _thread_obj(self, parent: str) -> dict:
        t = self.data["threads"][parent]
        replies = [m for m in t["messages"] if m["type"] != 21]
        return {"id": parent, "type": THREAD, "guild_id": self.data["guild"]["id"], "parent_id": t["channel"],
                "owner_id": t["messages"][0]["user"], "name": f"{CANARY} thread", "message_count": len(replies),
                "total_message_sent": len(replies),
                "last_message_id": max((m["id"] for m in replies), key=int, default=None),
                "thread_metadata": {"archived": t["archived"], "auto_archive_duration": 1440, "locked": False,
                                    "archive_timestamp": iso(parent), "create_timestamp": iso(parent)}}

    def _shown(self, m: dict, cid: str) -> dict:
        out = {"id": m["id"], "channel_id": cid, "type": m["type"], "content": m["content"],
               "author": self._user(m["user"], m["bot"]), "timestamp": iso(m["id"]), "edited_timestamp": None,
               "tts": False, "mention_everyone": False, "mentions": [], "attachments": [], "embeds": [],
               "pinned": False, "flags": 0}
        if m.get("ref"):
            ch = self.data["threads"][cid]["channel"] if cid in self.data["threads"] else cid
            out["message_reference"] = {"type": 0, "message_id": m["ref"], "channel_id": ch,
                                        "guild_id": self.data["guild"]["id"]}
            ref = self._message(ch, m["ref"])
            if ref is not None and m["type"] == 19:
                out["referenced_message"] = {"id": ref["id"], "content": ref["content"],
                                             "author": self._user(ref["user"], ref["bot"])}
        reactions = [{"emoji": {"id": None, "name": e}, "count": len(u), "count_details": {"burst": 0, "normal": len(u)},
                      "me": False, "me_burst": False, "burst_colors": []}
                     for e, u in (m.get("_reactions") or {}).items() if u]
        if reactions:
            out["reactions"] = reactions
        if m["id"] in self.data["threads"] and cid not in self.data["threads"]:
            out["thread"] = self._thread_obj(m["id"])
            out["flags"] = 32                    # HAS_THREAD
        return out

    def _messages(self, cid: str, q: dict[str, list[str]], is_thread: bool) -> tuple[int, Any]:
        try:
            after, before = int(one(q, "after", "0") or 0), int(one(q, "before", "0") or 0)
            limit = int(one(q, "limit", "50"))
        except ValueError:
            return 400, {"message": "Invalid Form Body", "code": 50035}
        if not 1 <= limit <= 100:
            return 400, {"message": "Invalid Form Body", "code": 50035}
        msgs = self.data["threads"][cid]["messages"] if is_thread else self.data["channels"][cid]
        got = sorted((m for m in msgs if int(m["id"]) > after and (not before or int(m["id"]) < before)),
                     key=lambda m: int(m["id"]))
        page = got[:limit] if after else got[-limit:]          # after: the next ones; else the newest
        return 200, [self._shown(m, cid) for m in reversed(page)]


def int_of(uid: str) -> int:
    """A user id as a number, for the order Discord lists reactions in (a fake's ids may be ``U001``)."""
    try:
        return int(uid)
    except ValueError:
        return int.from_bytes(uid.encode(), "big")
