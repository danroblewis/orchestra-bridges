"""What a ping is, and the events that say one came, was answered, or stopped waiting.

A **ping** is a message to the owner from someone else: a direct message (when ``dms``), a message
that mentions the owner, or one that mentions a configured group. Its events:

- ``slack.ping.received``: source the pinger, subject the thread, time the message's;
- ``slack.ping.answered``: source the owner, subject the thread (the rules are in ``bridge.py``);
- ``slack.ping.expired``: source the bridge, when a ping waited ``expire`` without an answer, or
  its message was deleted or cannot be read (``reason``). The system's own doing, so the stock balances:
  unanswered = received − answered − expired.

The subject is ``<workspace>/<channel>/<thread ts>``, so a ping and its answer share it and
Orchestra sees the hand-off. Ids are built from Slack's (``slack:<workspace>:<what>:<channel>:<ts>``,
where ``ts`` is the ping's), so re-reading never publishes twice. **Never text**: data holds the
workspace, channel and member ids, timestamps, the kind of ping, the rule that answered it and the
wait."""

from __future__ import annotations

from datetime import UTC, datetime

from .search import Message

RECEIVED, ANSWERED, EXPIRED = "slack.ping.received", "slack.ping.answered", "slack.ping.expired"
RANK = {RECEIVED: 0, ANSWERED: 1, EXPIRED: 2}
WHAT = {RECEIVED: "ping", ANSWERED: "answered", EXPIRED: "expired"}
TYPE_OF = {v: k for k, v in WHAT.items()}


def kind(m: Message, me: str, groups: list[str], dms: bool) -> str | None:
    """``dm``, ``mention`` or ``group`` when ``m`` is a ping to ``me``; None otherwise."""
    if not m.user or m.user == me:
        return None
    if dms and m.channel_type == "im":
        return "dm"
    if me in m.mentions:
        return "mention"
    if any(g in m.mentions for g in groups):
        return "group"
    return None


def prefix(team: str) -> str:
    """The start of every id this bridge gives an event of workspace ``team``."""
    return f"slack:{team}:"


def event_id(team: str, type_: str, channel: str, ts: str) -> str:
    return f"{prefix(team)}{WHAT[type_]}:{channel}:{ts}"


def parse_id(eid: str) -> tuple[str, str, str, str] | None:
    """(team, type, channel, ts) of an id ``event_id`` made; None for any other."""
    parts = eid.split(":")
    if len(parts) != 5 or parts[0] != "slack" or parts[2] not in TYPE_OF:
        return None
    return parts[1], TYPE_OF[parts[2]], parts[3], parts[4]


def subject(team: str, channel: str, thread: str) -> str:
    return f"{team}/{channel}/{thread}"


def iso_ts(ts: str) -> str:
    """A Slack timestamp (``1793000000.000100``) as RFC 3339, to the microsecond, without float error."""
    secs, _, frac = ts.partition(".")
    return datetime.fromtimestamp(int(secs), UTC).replace(microsecond=int((frac or "0").ljust(6, "0")[:6])).isoformat()


def iso(t: float) -> str:
    return datetime.fromtimestamp(t, UTC).isoformat()
