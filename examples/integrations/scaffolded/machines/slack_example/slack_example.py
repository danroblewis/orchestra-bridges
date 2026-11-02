"""slack_example: a poller that relays the threads of a few Slack channels, as who acted (scaffolded from
the orchestra-bridges kit ``slack``; edit freely, AGENTS.md says what it may do). A bot token
(``xoxb-``, the bot added to each channel) with ``channels:history`` and ``channels:read``, and for
a private channel ``groups:history`` and ``groups:read``; nothing else (``Bearer``). It never posts
(posting adds ``chat:write``, and is an Outbox draft's, never this poller's).

``python slack_example.py`` (the poller): each poll reads, per channel, its history over the threads it
follows (``conversations.history``: the top-level messages, each parent with its replies' summary
and its reactions) and, for a thread with a reply it has not seen, the thread
(``conversations.replies``). The channel's name and the workspace's address are read once a day
(``conversations.info``, ``auth.test``). It publishes, each once, with ``<channel>/<thread ts>`` as
the subject, and always as who acted (never as a source of its own):

- ``slack.thread.started``, a new top-level message, as its author (``person.slack.<member id>``,
  ``bot.slack.<id>`` for a bot's); data: ``channel``, ``channel_name``, ``user``, ``bot``,
  ``permalink`` (the thread's link);
- ``slack.thread.replied``, each reply, as who replied; data: ``channel``, ``channel_name``,
  ``permalink``, ``user``, ``author`` (the thread's), ``by_author``, ``bot``, ``first`` (the first
  reply by a person other than the author: neither the author's bump nor a bot's);
- ``slack.thread.checked``, the check emoji (``[settings] check_emoji``) added on the parent, as
  the first who added it; data: ``channel``, ``channel_name``, ``permalink``, ``by``, ``emoji``;
- ``slack.thread.unchecked``, the last check removed, as who removed it: only the one who added a
  reaction can remove it, so it is the one who had checked it (``by``); when several had and all
  removed theirs between two polls, Slack does not say whose was last, and it is published as the
  thread's author with ``by: "unknown"``;
- ``slack.message.posted``, each message, as before (data: channel, reply or not, bot or not).

Facts only: a response policy (how soon a thread is to be answered) is the workflow's setting,
which computes its own deadline from the thread's start; the poller adds none.

**Cadence and backoff** (per channel). Each poll reads every channel that is due: each poll by
default, or on its own interval (``[settings] cadence``, ``{ C0123 = "30m" }``). A channel Slack
answers 429 (rate limited) after the client's own retries and its ``Retry-After`` is left, and the
poll goes on with the others: it is due again after the poll's interval, doubling with each 429 in
a row up to ``max_backoff`` (1 hour), and back to its cadence after a good read. What it published
before the 429 is not published again (each event once).

**The cursor** (per channel): the newest top-level message seen and the threads followed (their
author, the newest reply seen, whether checked), for ``follow`` (2 days) after they started. The
first run starts at ``[settings] since`` (``now`` by default), never at the beginning of the
channel's history, and a restart never relays a thing twice (the ids are kept too).

``python slack_example.py measure`` (for a ``[[collect]]`` entry): prints, per channel's name,
``slack_channel_messages_last_hour{channel}``, ``slack_threads_awaiting_reply{channel}`` (the
threads of the last ``window`` with no reply by anyone but their author) and
``slack_threads_unchecked{channel}`` (without the check emoji).

It keeps metadata only: channel and member ids, the channel's name, timestamps, reaction names.
Never a message's text or a person's name."""

from __future__ import annotations

import math
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from orchestra.config import parse_duration

from orchestra_bridges.kits import runner
from orchestra_bridges.kits.client import HTTPFailure
from orchestra_bridges.kits.poller import actor_source, start_of

HERE = Path(__file__).resolve().parent
PAGE = 200
DAY = 86400.0
CHECK = "white_check_mark"


class SlackError(Exception):
    pass


def _ok(body: dict) -> dict:
    if not body.get("ok"):
        raise SlackError(str(body.get("error")))
    return body


def _message(m: dict) -> dict:
    return {"ts": m["ts"], "user": m.get("user") or m.get("bot_id"), "thread": m.get("thread_ts"),
            "bot": bool(m.get("bot_id")), "subtype": m.get("subtype"),
            "latest_reply": m.get("latest_reply"), "reply_users": list(m.get("reply_users") or []),
            "reactions": {r["name"]: list(r.get("users") or []) for r in m.get("reactions") or [] if r.get("name")}}


def keep_messages(body: dict) -> dict:
    """What is kept of a history or a thread: never a message's text."""
    _ok(body)
    return {"messages": [_message(m) for m in body.get("messages") or []],
            "next": (body.get("response_metadata") or {}).get("next_cursor") or ""}


def keep_channel(body: dict) -> dict:
    c = _ok(body)["channel"]
    return {"name": c.get("name") or c["id"]}


def pages(api, path: str, params: dict, ttl_s: float | None = None) -> list[dict]:
    out, cursor = [], ""
    while True:
        page = api.get(path, {**params, "limit": PAGE, "cursor": cursor or None}, keep=keep_messages, ttl_s=ttl_s)
        out += page["messages"]
        if not (cursor := page["next"]):
            return out


def history(api, channel: str, oldest: float, ttl_s: float | None = None) -> list[dict]:
    """The channel's top-level messages from ``oldest`` (inclusive), oldest first."""
    got = pages(api, "/api/conversations.history",
                {"channel": channel, "oldest": f"{oldest:.6f}", "inclusive": "true"}, ttl_s)
    return sorted((m for m in got if top_level(m)), key=lambda m: float(m["ts"]))


def replies(api, channel: str, ts: str, after: str) -> list[dict]:
    """The thread's replies after ``after``, oldest first (read fresh)."""
    got = pages(api, "/api/conversations.replies", {"channel": channel, "ts": ts, "oldest": after}, ttl_s=0)
    return sorted((m for m in got if m["ts"] != ts and float(m["ts"]) > float(after)), key=lambda m: float(m["ts"]))


THREADS = (None, "bot_message", "me_message", "file_share")   # a person's or a bot's message; no join, topic, pin


def top_level(m: dict) -> bool:
    """A thread's parent: neither a reply (a broadcast reply is one too) nor a channel notice."""
    return m["thread"] in (None, m["ts"]) and m["subtype"] in THREADS


def channel_name(api, channel: str) -> str:
    return api.get("/api/conversations.info", {"channel": channel}, keep=keep_channel, ttl_s=DAY)["name"]


def iso(ts: float | str) -> str:
    return datetime.fromtimestamp(float(ts), UTC).isoformat()


def who(m: dict) -> str:
    return actor_source("slack", m["user"], bot=m["bot"])


def check_emoji(settings) -> str:
    return settings.extra.get("check_emoji", CHECK)


def keep_workspace(body: dict) -> dict:
    return {"url": _ok(body).get("url") or ""}


def workspace(api) -> str:
    """The workspace's address (``https://<team>.slack.com/``), for a thread's link (``auth.test``
    needs no scope)."""
    return api.get("/api/auth.test", {}, keep=keep_workspace, ttl_s=DAY)["url"]


def permalink(url: str, channel: str, ts: str) -> str:
    return f"{url.rstrip('/')}/archives/{channel}/p{ts.replace('.', '')}" if url else ""


def make_poll(api, settings):
    channels = settings.extra.get("channels", [])
    emoji = check_emoji(settings)
    follow = parse_duration(settings.extra.get("follow"), 2 * DAY)
    cadence = {str(ch): parse_duration(v, 0.0) for ch, v in (settings.extra.get("cadence") or {}).items()}
    max_backoff = parse_duration(settings.extra.get("max_backoff"), 3600.0)

    def poll(p):
        now = time.time()
        cursor = p.cursor if isinstance(p.cursor, dict) else {}
        out = {}
        for ch in channels:
            st = dict(cursor.get(ch) or {})
            if now < float(st.get("due") or 0):
                out[ch] = st                    # not due yet: its cadence, or a backoff after a 429
                continue
            try:
                out[ch] = read_channel(p, api, ch, st, now, settings, emoji, follow)
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


def read_channel(p, api, ch: str, st: dict, now: float, settings, emoji: str, follow: float) -> dict:
    """One channel's read: its new threads, replies and checks; its cursor (no backoff: a good read)."""
    # No cursor yet: from [settings] since (now by default), never the channel's history.
    seen = float(st.get("seen") or math.floor(start_of(settings.extra.get("since"), now)))
    threads = {ts: dict(t) for ts, t in (st.get("threads") or {}).items() if float(ts) > now - follow}
    base = {"channel": ch, "channel_name": channel_name(api, ch)}
    url = workspace(api)
    # read fresh (ttl 0): a cached history would hide the newest reply and check
    for m in history(api, ch, min([seen, *map(float, threads)]), ttl_s=0):
        here = {**base, "permalink": permalink(url, ch, m["ts"])}
        if float(m["ts"]) > seen and m["ts"] not in threads:
            threads[m["ts"]] = started(p, m, here)
            seen = max(seen, float(m["ts"]))
        if m["ts"] in threads:
            follow_thread(p, api, m, threads[m["ts"]], here, emoji)
    return {"seen": seen, "threads": threads}


def started(p, m: dict, base: dict) -> dict:
    subject = f"{base['channel']}/{m['ts']}"
    data = {**base, "user": m["user"], "bot": m["bot"]}
    p.emit("slack.thread.started", subject, data, source=who(m), stamp="started", time=iso(m["ts"]))
    p.emit("slack.message.posted", subject, {"channel": base["channel"], "reply": False, "bot": m["bot"]},
           source=who(m), key=f"{base['channel']}/{m['ts']}", stamp="posted", time=iso(m["ts"]))
    return {"author": m["user"], "author_bot": m["bot"], "last": m["ts"], "answered": False, "checked": False,
            "by": [], "turns": 0}


def follow_thread(p, api, m: dict, t: dict, base: dict, emoji: str) -> None:
    """A followed thread's new replies (read only when its latest reply is new) and its check."""
    subject = f"{base['channel']}/{m['ts']}"
    if m["latest_reply"] and float(m["latest_reply"]) > float(t["last"]):
        for r in replies(api, base["channel"], m["ts"], t["last"]):
            by_author = r["user"] == t["author"]
            first = not by_author and not r["bot"] and not t["answered"]   # a bot's reply answers nobody
            data = {**base, "user": r["user"], "author": t["author"], "by_author": by_author, "bot": r["bot"],
                    "first": first}
            p.emit("slack.thread.replied", subject, data, source=who(r), key=f"{subject}/{r['ts']}", stamp="replied",
                   time=iso(r["ts"]))
            p.emit("slack.message.posted", subject, {"channel": base["channel"], "reply": True, "bot": r["bot"]},
                   source=who(r), key=f"{base['channel']}/{r['ts']}", stamp="posted", time=iso(r["ts"]))
            t["answered"] = t["answered"] or first
            t["last"] = r["ts"]
    by = m["reactions"].get(emoji) or []
    had, t["by"] = t.get("by") or [], list(by)
    if bool(by) != t["checked"]:
        t["turns"] += 1                   # the stamp: a check, removed, added again is three events
        t["checked"] = bool(by)
        if by:
            p.emit("slack.thread.checked", subject, {**base, "by": by[0], "emoji": emoji},
                   source=actor_source("slack", by[0]), stamp=f"checked:{t['turns']}")
        else:
            # only who added a reaction can remove it: one who had checked it removed it
            if len(had) == 1:
                remover, source = had[0], actor_source("slack", had[0])
            else:
                remover, source = "unknown", actor_source("slack", t["author"], bot=t.get("author_bot", False))
            p.emit("slack.thread.unchecked", subject, {**base, "by": remover, "emoji": emoji}, source=source,
                   stamp=f"unchecked:{t['turns']}")


def measure(api, settings, series) -> None:
    emoji = check_emoji(settings)
    window = parse_duration(settings.extra.get("window"), DAY)
    now = time.time()
    for ch in settings.extra.get("channels", []):
        name = channel_name(api, ch)
        msgs = history(api, ch, now - window)
        recent = sum(1 for m in msgs if float(m["ts"]) >= now - 3600)
        waiting = sum(1 for m in msgs if not set(m["reply_users"]) - {m["user"]})
        unchecked = sum(1 for m in msgs if not m["reactions"].get(emoji))
        labels = {"channel": name}
        series.gauge("slack_channel_messages_last_hour", recent, labels,
                     help="Top-level messages posted in the channel in the last hour.")
        series.gauge("slack_threads_awaiting_reply", waiting, labels,
                     help="Threads started in the window with no reply by anyone but their author.")
        series.gauge("slack_threads_unchecked", unchecked, labels,
                     help="Threads started in the window without the check emoji on them.")


def main(argv: list[str] | None = None) -> int:
    return runner.main(HERE, make_poll, argv, measure=measure)


if __name__ == "__main__":
    sys.exit(main())
