"""Reading messages from ``search.messages``, keeping metadata only.

A search match carries the message's text. It is looked at once, in ``message``, to see which of
the watched mentions it holds (``<@U...>`` for the owner, ``<!subteam^S...>`` for a group), and
then dropped: a ``Message`` holds ids, users, the channel's kind and timestamps, never text.

The searches, unless ``[bridges.slack] queries`` replaces them (``{me}`` is the owner's member id):

- ``<@{me}>``: messages that mention the owner;
- ``<!subteam^{group}>``: messages that mention each configured group;
- ``to:<@{me}>``: direct messages to the owner (when ``dms``);
- ``from:<@{me}>``: the owner's own messages, read for the reply rule.

Each is narrowed with ``after:<two days before the window, in UTC>`` and read newest first, page by
page, until a match is older than the window. Slack reads ``after:D`` as strictly after day ``D`` in
the searcher's own time zone, which the bridge does not know. Two days back covers every zone from
UTC-12 to UTC+14: the UTC date two days before ``t`` is earlier than ``t``'s date anywhere. The
filter only saves requests; the precise cut is each match's own time.
Whatever the searches return, the bridge decides what is a ping by its own rule (``pings.py``), so
a search that returns too much costs requests, never correctness. Whether Slack's search reads
these spellings the same way in every workspace is for the contract test to show."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import parse_qs, urlsplit

from .api import Slack

MAX_PAGES = 100                      # Slack serves no page past the hundredth


def mention_query(me: str) -> str:
    return f"<@{me}>"


def group_query(group: str) -> str:
    return f"<!subteam^{group}>"


def dm_query(me: str) -> str:
    return f"to:<@{me}>"


def own_query(me: str) -> str:
    return f"from:<@{me}>"


@dataclass(frozen=True)
class Message:
    channel: str
    ts: str                          # Slack's id for the message in its channel, and its time
    thread_ts: str | None            # the thread it is a reply in; None when it is not a reply
    user: str | None                 # member id
    username: str | None             # handle
    bot: bool
    channel_type: str                # im, mpim, group (private channel) or channel
    mentions: frozenset[str]         # which watched ids its text mentions

    @property
    def t(self) -> float:
        return float(self.ts)

    @property
    def thread(self) -> str:
        """The thread it belongs to: the parent's ts, or its own when it is not a reply."""
        return self.thread_ts or self.ts


def _channel_type(ch: dict) -> str:
    if ch.get("is_im"):
        return "im"
    if ch.get("is_mpim"):
        return "mpim"
    if ch.get("is_group") or ch.get("is_private"):
        return "group"
    return "channel"


def _thread_ts(match: dict) -> str | None:
    """The parent of a reply: a match's own ``thread_ts`` when Slack gives it, else the permalink's."""
    if t := match.get("thread_ts"):
        return None if t == match.get("ts") else str(t)
    q = parse_qs(urlsplit(match.get("permalink") or "").query)
    t = (q.get("thread_ts") or [None])[0]
    return None if t in (None, match.get("ts")) else t


def message(match: dict, watched: set[str]) -> Message | None:
    """A search match as metadata, or None when it is not a message (a file, say)."""
    ch = match.get("channel") or {}
    ts = match.get("ts")
    if not isinstance(ch, dict) or not ch.get("id") or not ts:
        return None
    text = match.get("text") or ""
    mentions = frozenset(w for w in watched if f"<@{w}" in text or f"<!subteam^{w}" in text)
    return Message(str(ch["id"]), str(ts), _thread_ts(match), match.get("user"), match.get("username"),
                   bool(match.get("bot_id")), _channel_type(ch), mentions)


AFTER_DAYS = 2                       # how many days back ``after:`` reaches, for any time zone


def day_before(t: float) -> str:
    """The day for ``after:``: one that ends before ``t`` in every time zone (UTC-12 to UTC+14)."""
    return datetime.fromtimestamp(t - AFTER_DAYS * 86400, UTC).strftime("%Y-%m-%d")


def since(api: Slack, query: str, t: float, watched: set[str]) -> list[Message]:
    """Every message ``query`` finds at or after ``t``, oldest first."""
    out: dict[tuple[str, str], Message] = {}
    q = f"{query} after:{day_before(t)}"
    for page in range(1, MAX_PAGES + 1):
        got = api.search(q, page)
        matches = got.get("matches") or []
        older = False
        for m in matches:
            msg = message(m, watched)
            if msg is None:
                continue
            if msg.t < t:
                older = True
                continue
            out[(msg.channel, msg.ts)] = msg
        pages = (got.get("paging") or {}).get("pages") or 1
        if older or not matches or page >= pages:
            break
    return sorted(out.values(), key=lambda m: (m.t, m.channel))
