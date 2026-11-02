"""slack_watcher: a poller that relays the messages posted in a few Slack channels, and follows the
mentions of a few members until they answer (built with ``orchestra bridges scaffold slack
slack_watcher``, then completed; AGENTS.md says what it may do). A bot or user token with
``channels:history`` and ``users:read`` (``Bearer``).

``python slack_watcher.py`` (the poller): each poll reads each channel's history since its last
message seen (``conversations.history``, ``oldest``) and the replies of its threads of the last
``lookback_s`` with a newer reply (``conversations.replies``: Slack's history has no replies) and publishes ``slack.message.posted`` once per
message, as whoever posted it (``person.slack.<member id>``, ``bot.slack.<id>`` for a bot), with
``<channel>/<thread ts>`` as the subject (a reply's thread, or the message's own ts). Its data:
the channel, whether it is a reply or a bot's, and which watched members it mentions.

**Mentions.** ``[settings] watch`` names members (ids). A message mentioning one of them
(``<@U001>`` in its text, read in memory only) opens a mention in its thread, and that member's
own message in the thread answers it. ``publish = "mentions"`` filters what is published to the
messages that mention a watched member and the watched members' answers; ``"all"`` (the default)
publishes every message.

Counted for the measurement, after each good poll, with the cursor (a restart neither loses nor
repeats a count): ``slack_messages_total{channel}`` (every message read, published or not),
``slack_mentions_total`` (mentions opened) and ``slack_mentions_answered_total`` (mentions answered).

``python slack_watcher.py measure`` (for a ``[[collect]]`` entry): prints those counters and
``slack_mentions_open``, a gauge: the mentions not yet answered (opened less answered).

It keeps metadata only: channel and member ids, timestamps, whether a message is a reply or a
bot's, the ids it mentions. Never a message's text or a person's name."""

from __future__ import annotations

import re
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from orchestra_bridges.kits import runner
from orchestra_bridges.kits.poller import actor_source
from orchestra_bridges.kits.pollstate import counters_at

HERE = Path(__file__).resolve().parent
PAGE = 200
DAY = 86400.0
MENTION = re.compile(r"<@([UW][A-Z0-9]+)(?:\|[^>]*)?>")
OPENED, ANSWERED, MESSAGES = "slack_mentions_total", "slack_mentions_answered_total", "slack_messages_total"


class SlackError(Exception):
    pass


def keep_history(body: dict) -> dict:
    """What is kept of a channel's history: never a message's text, only the ids it mentions."""
    if not body.get("ok"):
        raise SlackError(str(body.get("error")))
    return {"messages": [{"ts": m["ts"], "user": m.get("user") or m.get("bot_id"), "thread": m.get("thread_ts"),
                          "bot": bool(m.get("bot_id")), "mentions": sorted(set(MENTION.findall(m.get("text") or ""))),
                          "latest_reply": m.get("latest_reply")}
                         for m in body.get("messages") or []],
            "next": (body.get("response_metadata") or {}).get("next_cursor") or ""}


def keep_user(body: dict) -> dict:
    if not body.get("ok"):
        return {"bot": False}
    return {"bot": bool(body["user"].get("is_bot"))}


def history(api, channel: str, oldest: float, ttl_s: float | None = None) -> list[dict]:
    out, cursor = [], ""
    while True:
        page = api.get("/api/conversations.history", {"channel": channel, "oldest": f"{oldest:.6f}", "limit": PAGE,
                                                      "cursor": cursor or None}, keep=keep_history, ttl_s=ttl_s)
        out += page["messages"]
        if not (cursor := page["next"]):
            return out


def replies(api, channel: str, ts: str, after: str) -> list[dict]:
    """A thread's replies after ``after`` (Slack's history holds only the channel's top-level
    messages; a thread's replies are read with ``conversations.replies``), oldest first."""
    out, cursor = [], ""
    while True:
        page = api.get("/api/conversations.replies", {"channel": channel, "ts": ts, "oldest": after, "limit": PAGE,
                                                      "cursor": cursor or None}, keep=keep_history, ttl_s=0)
        out += [m for m in page["messages"] if m["ts"] != ts and float(m["ts"]) > float(after)]
        if not (cursor := page["next"]):
            return out


def messages(api, ch: str, oldest: float, lookback: float, last: dict) -> list[dict]:
    """The channel's new messages, replies included: its top-level messages after ``oldest``, and
    each thread's replies after the last one read (``last``: thread ts -> reply ts), for the
    threads of the last ``lookback`` whose latest reply is newer."""
    out = []
    for m in history(api, ch, min(oldest, time.time() - lookback), ttl_s=0):
        if m["thread"] not in (None, m["ts"]):
            if float(m["ts"]) > oldest:
                out.append(m)                    # a reply in the history (a broadcast): as it comes
            continue
        if float(m["ts"]) > oldest:
            out.append(m)
        after = last.get(m["ts"], m["ts"])
        if m["latest_reply"] and float(m["latest_reply"]) > float(after):
            got = replies(api, ch, m["ts"], after)
            out += [r for r in got if r["ts"] not in {x["ts"] for x in out}]
            last[m["ts"]] = max([after, *(r["ts"] for r in got)], key=float)
    return out


def make_poll(api, settings):
    channels = settings.extra.get("channels", [])
    watch = set(settings.extra.get("watch", []))
    only_mentions = settings.extra.get("publish", "all") == "mentions"
    lookback = float(settings.extra.get("lookback_s", DAY))

    def poll(p):
        cursor = p.cursor if isinstance(p.cursor, dict) and "seen" in p.cursor else {"seen": {}, "open": {}}
        seen, open_ = dict(cursor["seen"]), {k: list(v) for k, v in cursor["open"].items()}
        last = {k: dict(v) for k, v in (cursor.get("replied") or {}).items()}
        for ch in channels:
            oldest = float(seen.get(ch) or time.time() - lookback)
            for m in sorted(messages(api, ch, oldest, lookback, last.setdefault(ch, {})), key=lambda m: float(m["ts"])):
                thread = f"{ch}/{m['thread'] or m['ts']}"
                p.counter(MESSAGES, 1, {"channel": ch}, help="Slack messages posted in the channel.")
                mentioned = sorted(watch & set(m["mentions"]) - {m["user"]})
                answers = m["user"] in open_.get(thread, [])
                if answers:
                    open_[thread].remove(m["user"])
                    p.counter(ANSWERED, 1, help="Mentions of a watched member that the member answered in the thread.")
                for who in mentioned:
                    if who not in open_.setdefault(thread, []):
                        open_[thread].append(who)
                        p.counter(OPENED, 1, help="Mentions of a watched member (opened).")
                open_ = {k: v for k, v in open_.items() if v}
                seen[ch] = max(float(seen.get(ch) or 0), float(m["ts"]))
                if only_mentions and not (mentioned or answers):
                    continue
                bot = m["bot"] or (m["user"] and api.get("/api/users.info", {"user": m["user"]}, keep=keep_user,
                                                          ttl_s=DAY)["bot"])
                p.emit("slack.message.posted", thread,
                       {"channel": ch, "reply": bool(m["thread"] and m["thread"] != m["ts"]), "bot": bool(bot),
                        "mentions": mentioned, "answers": answers},
                       source=actor_source("slack", m["user"], bot=bool(bot)), key=f"{ch}/{m['ts']}", stamp="posted",
                       time=datetime.fromtimestamp(float(m["ts"]), UTC).isoformat())
        horizon = time.time() - lookback
        p.cursor = {"seen": seen, "open": open_,
                    "replied": {ch: {t: r for t, r in v.items() if float(t) > horizon} for ch, v in last.items()}}

    return poll


def measure(api, settings, series) -> None:
    series.load_counters(counters_at(settings.poll_dir))
    total = {name: sum(t.values()) for name, t in series.counters.items()}
    series.gauge("slack_mentions_open", total.get(OPENED, 0) - total.get(ANSWERED, 0),
                 help="Mentions of a watched member not yet answered in their thread (a level).")


def main(argv: list[str] | None = None) -> int:
    return runner.main(HERE, make_poll, argv, measure=measure)


if __name__ == "__main__":
    sys.exit(main())
