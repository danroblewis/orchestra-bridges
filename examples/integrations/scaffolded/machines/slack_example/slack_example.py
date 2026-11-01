"""slack_example: a poller that relays the messages posted in a few Slack channels (scaffolded from the
orchestra-bridges kit ``slack``; edit freely, AGENTS.md says what it may do). A bot or user token
with ``channels:history`` and ``users:read`` (``Bearer``).

``python slack_example.py`` (the poller): each poll reads each channel's history since its last message
seen (``conversations.history``, ``oldest``), and publishes ``slack.message.posted`` once per
message, as whoever posted it (``person.slack.<member id>``, ``bot.slack.<id>`` for a bot), with
``<channel>/<thread ts>`` as the subject (a reply's thread, or the message's own ts). Whether a
member is a bot is read once (``users.info``, cached a day).

``python slack_example.py measure`` (for a ``[[collect]]`` entry): prints
``slack_channel_messages_last_hour{channel}``.

It keeps metadata only: channel and member ids, timestamps, whether a message is a reply or a
bot's. Never a message's text or a person's name."""

from __future__ import annotations

import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from orchestra_bridges.kits import runner
from orchestra_bridges.kits.poller import actor_source

HERE = Path(__file__).resolve().parent
PAGE = 200
DAY = 86400.0


class SlackError(Exception):
    pass


def keep_history(body: dict) -> dict:
    """What is kept of a channel's history: never a message's text."""
    if not body.get("ok"):
        raise SlackError(str(body.get("error")))
    return {"messages": [{"ts": m["ts"], "user": m.get("user") or m.get("bot_id"), "thread": m.get("thread_ts"),
                          "bot": bool(m.get("bot_id"))} for m in body.get("messages") or []],
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


def make_poll(api, settings):
    channels = settings.extra.get("channels", [])
    lookback = float(settings.extra.get("lookback_s", DAY))

    def poll(p):
        seen = dict(p.cursor or {})
        for ch in channels:
            oldest = float(seen.get(ch) or time.time() - lookback)
            for m in sorted(history(api, ch, oldest, ttl_s=0), key=lambda m: float(m["ts"])):
                bot = m["bot"] or (m["user"] and api.get("/api/users.info", {"user": m["user"]}, keep=keep_user,
                                                          ttl_s=DAY)["bot"])
                p.emit("slack.message.posted", f"{ch}/{m['thread'] or m['ts']}",
                       {"channel": ch, "reply": bool(m["thread"] and m["thread"] != m["ts"]), "bot": bool(bot)},
                       source=actor_source("slack", m["user"], bot=bool(bot)), key=f"{ch}/{m['ts']}", stamp="posted",
                       time=datetime.fromtimestamp(float(m["ts"]), UTC).isoformat())
                seen[ch] = max(float(seen.get(ch) or 0), float(m["ts"]))
        p.cursor = seen

    return poll


def measure(api, settings, series) -> None:
    for ch in settings.extra.get("channels", []):
        n = len(history(api, ch, time.time() - 3600))
        series.gauge("slack_channel_messages_last_hour", n, {"channel": ch},
                     help="Messages posted in the channel in the last hour.")


def main(argv: list[str] | None = None) -> int:
    return runner.main(HERE, make_poll, argv, measure=measure)


if __name__ == "__main__":
    sys.exit(main())
