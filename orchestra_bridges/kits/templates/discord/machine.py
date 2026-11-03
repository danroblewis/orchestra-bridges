"""{{name}}: a poller that relays the threads of a few Discord channels, as who acted (scaffolded from
the orchestra-bridges kit ``discord``; edit freely, AGENTS.md says what it may do). A bot token (the
bot added to the guild, ``Authorization: Bot <token>``) with **View Channel** and **Read Message
History** on each channel; nothing else. It never posts (posting adds Send Messages, and is an Outbox
draft's, never this poller's), and it needs no Message Content intent: it reads no text.

The same facts as the ``slack`` kit, in the same shapes, so a workflow that waits on the Slack
kit's events waits on these with ``discord.`` for ``slack.`` (and ``outside: "discord"``). A
**thread** is a channel's message that is not a reply: its replies are the messages in the Discord
thread started from it and the channel's inline replies to it (or to one of its replies).

``python {{name}}.py`` (the poller): each poll reads, per channel, its messages over the threads it
follows (``GET /channels/{id}/messages``, forward from an ``after`` cursor: each parent with its
reactions' counts and the thread started from it), the guild's active threads and the channel's
archived public ones (``/guilds/{guild}/threads/active``, ``/channels/{id}/threads/archived/public``:
each thread's newest message), and, for a thread with a message it has not seen, the thread
(``/channels/{thread}/messages``). The channel's name and its guild are read once a day
(``/channels/{id}``); who reacted only when a reaction's count changes
(``/channels/{id}/messages/{message}/reactions/{emoji}``). It publishes, each once, with
``<channel>/<message id>`` (the thread's parent) as the subject, and always as who acted (never as
a source of its own):

- ``discord.thread.started``, a new message that is not a reply, as its author
  (``person.discord.<user id>``, ``bot.discord.<id>`` for a bot's or a webhook's); data:
  ``channel``, ``channel_name``, ``user``, ``bot``, ``permalink`` (the parent's link,
  ``https://discord.com/channels/<guild>/<channel>/<message>``);
- ``discord.thread.replied``, each reply, as who replied; data: ``channel``, ``channel_name``,
  ``permalink``, ``user``, ``author`` (the thread's), ``by_author``, ``bot``, ``first`` (the first
  reply by a person other than the author: neither the author's bump nor a bot's);
- ``discord.thread.checked``, the check emoji (``[settings] check_emoji``, ✅) added on the parent,
  as who added it; data: ``channel``, ``channel_name``, ``permalink``, ``by``, ``emoji`` (Slack's
  name for it, ``white_check_mark`` for ✅, so a workflow's ``check_emoji`` setting is the same);
- ``discord.thread.unchecked``, the last check removed, as who removed it: only the one who added a
  reaction can remove it (a moderator aside), so it is the one who had checked it (``by``); when
  several had and all removed theirs between two polls, it is published as the thread's author with
  ``by: "unknown"``;
- ``discord.message.posted``, each message (data: channel, reply or not, bot or not); an inline
  reply to a message it does not follow is this alone.

Facts only: a response policy is the workflow's setting; the poller adds none.

**Cadence and backoff** (per channel), as the Slack kit's. Each poll reads every channel that is
due: each poll by default, or on its own interval (``[settings] cadence``). Discord's rate limits
are honoured by the client: a bucket it says is empty (``X-RateLimit-Remaining: 0``) is waited out
for its ``X-RateLimit-Reset-After``, and a 429's ``Retry-After`` is waited (up to the client's
limit). A channel still answered 429 is left for the poll's interval, doubling with each 429 in a
row up to ``max_backoff`` (1 hour), and the poll goes on with the others.

**The cursor** (per channel): the newest message seen (a snowflake), the threads followed (their
author, the newest reply seen, whether checked, who checked) for ``follow`` (2 days) after they
started, and the inline replies to them. The first run starts at ``[settings] since`` (``now`` by
default), never at the beginning of the channel's history, and a restart never relays a thing twice.

``python {{name}}.py measure`` (for a ``[[collect]]`` entry): prints, per channel's name,
``discord_channel_messages_last_hour{channel}``, ``discord_threads_awaiting_reply{channel}`` (the
threads of the last ``window`` with no reply by anyone but their author) and
``discord_threads_unchecked{channel}`` (without the check emoji).

``python {{name}}.py channels``: the guild's channels (``[settings] guild``), one a line: id, type,
``#name``; read-only, names and ids only, for the owner to pick ``channels`` from. A text or an
announcement channel is read; a forum's posts are threads with no parent message, not read here.

It keeps metadata only: channel, message and user ids, the channel's name, reaction counts and
names. Never a message's text, a thread's name or a person's name."""

from __future__ import annotations

import math
import sys
import time
import urllib.parse
from datetime import UTC, datetime
from pathlib import Path

from orchestra.config import parse_duration

from orchestra_bridges.kits import runner
from orchestra_bridges.kits.client import HTTPFailure
from orchestra_bridges.kits.poller import actor_source, start_of

HERE = Path(__file__).resolve().parent
API = "/api/v10"
PAGE = 100
DAY = 86400.0
EPOCH_MS = 1420070400000                     # Discord's epoch (2015-01-01) in a snowflake's milliseconds
CHECK = "✅"
SHORTCODES = {"✅": "white_check_mark", "✔️": "heavy_check_mark", "☑️": "ballot_box_with_check"}
MESSAGES = (0, 19, 20, 23)                   # a person's or a bot's: default, reply, slash and app commands
REPLY = 19
TYPES = {0: "text", 2: "voice", 4: "category", 5: "announcement", 13: "stage", 15: "forum", 16: "media"}
USER_AGENT = "DiscordBot (https://github.com/danroblewis/orchestra-bridges, 1)"   # Discord asks for this form


class DiscordError(Exception):
    pass


def snowflake(t: float) -> str:
    """The smallest message id at time ``t`` (an ``after`` cursor)."""
    return str(max(0, int(t * 1000) - EPOCH_MS) << 22)


def time_of(sid: str) -> float:
    return ((int(sid) >> 22) + EPOCH_MS) / 1000.0


def iso(sid: str) -> str:
    return datetime.fromtimestamp(time_of(sid), UTC).isoformat()


def _list(body) -> list:
    if not isinstance(body, list):
        raise DiscordError(f"expected a list, got {type(body).__name__}")
    return body


def _thread(t: dict | None) -> dict | None:
    if not t:
        return None
    return {"id": t["id"], "parent": t.get("parent_id"), "last": t.get("last_message_id"),
            "count": int(t.get("message_count") or 0)}


def _message(m: dict) -> dict:
    a = m.get("author") or {}
    ref = (m.get("message_reference") or {}).get("message_id")
    return {"id": m["id"], "user": a.get("id"), "bot": bool(a.get("bot") or m.get("webhook_id")),
            "type": int(m.get("type") or 0), "ref": ref if m.get("type") == REPLY else None,
            "thread": _thread(m.get("thread")),
            "reactions": {r["emoji"]["name"]: {"count": int(r.get("count") or 0),
                                               "key": f"{r['emoji']['name']}:{r['emoji']['id']}" if r["emoji"].get("id")
                                               else r["emoji"]["name"]}
                          for r in m.get("reactions") or [] if (r.get("emoji") or {}).get("name")}}


def keep_messages(body) -> list[dict]:
    """What is kept of a page of messages: never a message's text or an author's name."""
    return [_message(m) for m in _list(body)]


def keep_channel(body: dict) -> dict:
    return {"name": body.get("name") or body["id"], "guild": body.get("guild_id") or "", "type": body.get("type")}


def keep_threads(body: dict) -> list[dict]:
    """A thread listing: each thread's id, parent channel and newest message; never its name."""
    return [_thread(t) for t in (body or {}).get("threads") or []]


def keep_users(body) -> list[dict]:
    return [{"id": u["id"], "bot": bool(u.get("bot"))} for u in _list(body)]


def keep_channels(body) -> list[dict]:
    return [{"id": c["id"], "name": c.get("name") or "", "type": c.get("type"), "position": c.get("position") or 0}
            for c in _list(body)]


def messages(api, cid: str, after: str, ttl_s: float | None = 0) -> list[dict]:
    """The channel's (or thread's) messages after the id ``after``, oldest first (read fresh)."""
    out = []
    while True:
        page = api.get(f"{API}/channels/{cid}/messages", {"after": after, "limit": PAGE}, keep=keep_messages,
                       ttl_s=ttl_s)
        out += page
        if len(page) < PAGE:
            return sorted(out, key=lambda m: int(m["id"]))
        after = max((m["id"] for m in page), key=int)


def channel_info(api, cid: str) -> dict:
    return api.get(f"{API}/channels/{cid}", {}, keep=keep_channel, ttl_s=DAY)


def threads_of(api, cid: str, guild: str, memo: dict) -> dict[str, dict]:
    """The channel's threads by their parent message's id (a thread started from a message has its
    id): the guild's active threads (read once a poll) and the channel's archived public ones."""
    if guild and guild not in memo:
        memo[guild] = api.get(f"{API}/guilds/{guild}/threads/active", {}, keep=keep_threads, ttl_s=0)
    archived = api.get(f"{API}/channels/{cid}/threads/archived/public", {"limit": PAGE}, keep=keep_threads, ttl_s=0)
    return {t["id"]: t for t in [*memo.get(guild, []), *archived] if t["parent"] == cid}


def thread_replies(api, tid: str, after: str) -> list[dict]:
    """The thread's messages after ``after``, oldest first; not its starter (type 21) or a notice."""
    return [m for m in messages(api, tid, after) if m["type"] in MESSAGES]


def reaction_users(api, cid: str, mid: str, key: str) -> list[dict]:
    return api.get(f"{API}/channels/{cid}/messages/{mid}/reactions/{urllib.parse.quote(key, safe=':')}",
                   {"limit": PAGE}, keep=keep_users, ttl_s=0)


def check_emoji(settings) -> str:
    """The reaction that marks a thread done, as Discord names it (``✅``; Slack's name is taken too)."""
    v = str(settings.extra.get("check_emoji") or CHECK)
    return next((e for e, name in SHORTCODES.items() if name == v), v)


def permalink(guild: str, cid: str, mid: str) -> str:
    return f"https://discord.com/channels/{guild}/{cid}/{mid}" if guild else ""


def who(m: dict) -> str:
    return actor_source("discord", m["user"], bot=m["bot"])


def make_poll(api, settings):
    channels = [str(c) for c in settings.extra.get("channels", [])]
    emoji = check_emoji(settings)
    follow = parse_duration(settings.extra.get("follow"), 2 * DAY)
    cadence = {str(ch): parse_duration(v, 0.0) for ch, v in (settings.extra.get("cadence") or {}).items()}
    max_backoff = parse_duration(settings.extra.get("max_backoff"), 3600.0)

    def poll(p):
        now = time.time()
        cursor = p.cursor if isinstance(p.cursor, dict) else {}
        out, memo = {}, {}
        for ch in channels:
            st = dict(cursor.get(ch) or {})
            if now < float(st.get("due") or 0):
                out[ch] = st                    # not due yet: its cadence, or a backoff after a 429
                continue
            try:
                out[ch] = read_channel(p, api, ch, st, now, settings, emoji, follow, memo)
            except HTTPFailure as e:
                if e.status != 429:
                    raise
                back = min(max_backoff, max(p.every_s, 2 * float(st.get("backoff") or 0)))
                print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {p.name}: {ch} rate limited (429): next read in "
                      f"{back:g}s", file=sys.stderr, flush=True)
                out[ch] = {**st, "backoff": back, "due": now + back}
                continue
            if cadence.get(ch):
                out[ch]["due"] = now + cadence[ch]
        p.cursor = out

    return poll


def read_channel(p, api, ch: str, st: dict, now: float, settings, emoji: str, follow: float, memo: dict) -> dict:
    """One channel's read: its new threads, replies and checks; its cursor (no backoff: a good read)."""
    # No cursor yet: from [settings] since (now by default), never the channel's history.
    seen = str(st.get("seen") or snowflake(math.floor(start_of(settings.extra.get("since"), now))))
    threads = {pid: dict(t) for pid, t in (st.get("threads") or {}).items() if time_of(pid) > now - follow}
    inline = {mid: pid for mid, pid in (st.get("inline") or {}).items() if pid in threads}
    info = channel_info(api, ch)
    guild = str(settings.extra.get("guild") or info["guild"])
    base = {"channel": ch, "channel_name": info["name"]}
    listed = threads_of(api, ch, guild, memo)
    after = str(min([int(seen), *(int(pid) - 1 for pid in threads)]))
    for m in messages(api, ch, after):
        if m["type"] not in MESSAGES:
            continue                            # a join, a pin, a thread's notice
        parent = (m["ref"] if m["ref"] in threads else inline.get(m["ref"])) if m["ref"] else None
        if int(m["id"]) > int(seen):
            if parent:
                replied(p, m, parent, threads[parent], {**base, "permalink": permalink(guild, ch, parent)})
                inline[m["id"]] = parent
            elif m["ref"]:                      # a reply to a message not followed: a message, no thread
                p.emit("discord.message.posted", f"{ch}/{m['id']}", {"channel": ch, "reply": True, "bot": m["bot"]},
                       source=who(m), key=f"{ch}/{m['id']}", stamp="posted", time=iso(m["id"]))
            elif m["id"] not in threads:
                threads[m["id"]] = started(p, m, {**base, "permalink": permalink(guild, ch, m["id"])})
            seen = max(seen, m["id"], key=int)
        if m["id"] in threads:
            follow_thread(p, api, m, threads[m["id"]], {**base, "permalink": permalink(guild, ch, m["id"])}, emoji,
                          m["thread"] or listed.get(m["id"]))
    return {"seen": seen, "threads": threads, "inline": inline}


def started(p, m: dict, base: dict) -> dict:
    subject = f"{base['channel']}/{m['id']}"
    p.emit("discord.thread.started", subject, {**base, "user": m["user"], "bot": m["bot"]}, source=who(m),
           stamp="started", time=iso(m["id"]))
    p.emit("discord.message.posted", subject, {"channel": base["channel"], "reply": False, "bot": m["bot"]},
           source=who(m), key=subject, stamp="posted", time=iso(m["id"]))
    return {"author": m["user"], "author_bot": m["bot"], "last": m["id"], "answered": False, "checked": False,
            "by": [], "bots": [], "turns": 0}


def replied(p, r: dict, parent: str, t: dict, base: dict) -> None:
    subject = f"{base['channel']}/{parent}"
    by_author = r["user"] == t["author"]
    first = not by_author and not r["bot"] and not t["answered"]   # a bot's reply answers nobody
    data = {**base, "user": r["user"], "author": t["author"], "by_author": by_author, "bot": r["bot"], "first": first}
    p.emit("discord.thread.replied", subject, data, source=who(r), key=f"{subject}/{r['id']}", stamp="replied",
           time=iso(r["id"]))
    p.emit("discord.message.posted", subject, {"channel": base["channel"], "reply": True, "bot": r["bot"]},
           source=who(r), key=f"{base['channel']}/{r['id']}", stamp="posted", time=iso(r["id"]))
    t["answered"] = t["answered"] or first


def follow_thread(p, api, m: dict, t: dict, base: dict, emoji: str, thread: dict | None) -> None:
    """A followed thread's new replies in its Discord thread (read only when its newest message is
    new) and its check (who reacted, read only when the count changed)."""
    if thread and thread["last"] and int(thread["last"]) > int(t["last"]):
        for r in thread_replies(api, thread["id"], t["last"]):
            replied(p, r, m["id"], t, base)
            t["last"] = r["id"]
    reaction = m["reactions"].get(emoji)
    count = reaction["count"] if reaction else 0
    had, bots = list(t.get("by") or []), set(t.get("bots") or [])
    if count == len(had) and bool(count) == t["checked"]:
        return                                  # no change since the last read
    users = reaction_users(api, base["channel"], m["id"], reaction["key"]) if count else []
    t["by"], t["bots"] = [u["id"] for u in users], [u["id"] for u in users if u["bot"]]
    by = t["by"]
    if bool(by) == t["checked"]:
        return
    t["turns"] += 1                             # the stamp: a check, removed, added again is three events
    t["checked"] = bool(by)
    shown = SHORTCODES.get(emoji, emoji)
    subject = f"{base['channel']}/{m['id']}"
    if by:
        first = next((u for u in by if u not in had), by[0])   # Discord lists who reacted by id, not by time
        p.emit("discord.thread.checked", subject, {**base, "by": first, "emoji": shown},
               source=actor_source("discord", first, bot=first in t["bots"]), stamp=f"checked:{t['turns']}")
    else:
        # only who added a reaction can remove it: one who had checked it removed it
        if len(had) == 1:
            remover, source = had[0], actor_source("discord", had[0], bot=had[0] in bots)
        else:
            remover, source = "unknown", actor_source("discord", t["author"], bot=t.get("author_bot", False))
        p.emit("discord.thread.unchecked", subject, {**base, "by": remover, "emoji": shown}, source=source,
               stamp=f"unchecked:{t['turns']}")


def measure(api, settings, series) -> None:
    emoji = check_emoji(settings)
    window = parse_duration(settings.extra.get("window"), DAY)
    now = time.time()
    for ch in (str(c) for c in settings.extra.get("channels", [])):
        info = channel_info(api, ch)
        msgs = [m for m in messages(api, ch, snowflake(now - window), ttl_s=None) if m["type"] in MESSAGES]
        tops = [m for m in msgs if not m["ref"]]
        answered: dict[str, set] = {m["id"]: set() for m in tops}
        for m in msgs:
            if m["ref"] in answered:
                answered[m["ref"]].add(m["user"])
        listed = threads_of(api, ch, str(settings.extra.get("guild") or info["guild"]), {})
        for m in tops:
            thread = m["thread"] or listed.get(m["id"])
            if thread and thread["count"]:
                answered[m["id"]] |= {r["user"] for r in thread_replies(api, thread["id"], m["id"])}
        recent = sum(1 for m in tops if time_of(m["id"]) >= now - 3600)
        waiting = sum(1 for m in tops if not answered[m["id"]] - {m["user"]})
        unchecked = sum(1 for m in tops if not (m["reactions"].get(emoji) or {}).get("count"))
        labels = {"channel": info["name"]}
        series.gauge("discord_channel_messages_last_hour", recent, labels,
                     help="Messages that are not replies posted in the channel in the last hour.")
        series.gauge("discord_threads_awaiting_reply", waiting, labels,
                     help="Threads started in the window with no reply by anyone but their author.")
        series.gauge("discord_threads_unchecked", unchecked, labels,
                     help="Threads started in the window without the check emoji on them.")


def list_channels(api, settings) -> list[str]:
    """The guild's channels, one a line: id, type, #name (read-only; names and ids only)."""
    guild = str(settings.extra.get("guild") or "")
    if not guild:
        raise DiscordError("[settings] guild: the guild's id (Discord: Server Settings, Widget, or Copy Server ID)")
    chans = api.get(f"{API}/guilds/{guild}/channels", {}, keep=keep_channels, ttl_s=0)
    order = sorted(chans, key=lambda c: (c["type"] not in (0, 5), c["type"], c["position"], c["name"]))
    return [f"{c['id']}\t{TYPES.get(c['type'], str(c['type']))}\t#{c['name']}" for c in order]


def main(argv: list[str] | None = None) -> int:
    return runner.main(HERE, make_poll, argv, measure=measure, auth_format="Bot {token}",
                       headers={"User-Agent": USER_AGENT}, commands={"channels": list_channels})


if __name__ == "__main__":
    sys.exit(main())
