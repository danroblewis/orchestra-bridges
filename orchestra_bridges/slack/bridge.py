"""The Slack pings bridge: polls ``search.messages`` with the owner's user token and publishes the
pings waiting for the owner, and their answers, on the project's Orchestra bus.

Each poll first reads, then publishes, so a poll that fails part way publishes nothing it would
publish again differently:

1. the searches (``search.py``) over the window: pings, and the owner's own messages when the reply
   rule is on;
2. ``reactions.get`` for every ping that waits, when the reaction rule is on.

Then, holding the lock the ``/metrics`` server reads under:

- each new ping is published as ``slack.ping.received`` (source the pinger, time the message's);
- **the reply rule**: the owner's first message after a ping in its thread (or in its channel after
  it, when the ping was not in a thread) answers it, at that message's time;
- a ping that has waited ``expire`` stops waiting (``slack.ping.expired``, at the moment it ran out);
- **the reaction rule** (the default): a checkmark the owner put on a ping answers it, and every
  earlier ping still waiting in its thread. Slack does not say when a reaction was added, so the
  answer is stamped when the bridge first saw it: up to a poll late, or at the end of a gap. Its
  data says so (``observed``) and gives ``not_before``, the last poll that did not see it.
- a ping whose message is gone (``message_not_found``) stops waiting, as expired.

**It keeps no state of its own.** On start it reads its own events back from the bus (ids starting
``slack:<workspace>:``) and rebuilds the waiting pings and the counts from them; each counter then
continues from what the store last holds for it, if that is more (``waits.py``). It reads Slack
again from the later of its last event and the store's last scrape of it, less ``overlap``; never
further back than ``max_catchup``. With nothing on either, it reads from ``lookback`` ago. A poll
that succeeds more than three rounds after the last good one (the laptop slept, the bridge was
stopped, Slack or the network was down) records the gap once, as ``bridge.gap`` {workspace, from,
to, recovered}, and reads back to where it left off.

The bridge itself is the machine ``bridges.slack``. Its series reach the store through a
``[[scrape]]`` of the same name, which labels them ``machine="bridges.slack"``."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
import time
from collections.abc import Callable

from orchestra.events import Events, event_time
from orchestra.metrics import MetricsStore

from ..config import SlackConfig
from ..identity import Actor, People
from . import search
from .api import Slack, SlackError
from .pings import ANSWERED, EXPIRED, RANK, RECEIVED, event_id, iso, iso_ts, kind, parse_id, subject
from .serve import MetricsServer
from .waits import COUNTERS, LEVEL, Waits

MACHINE = "bridges.slack"
SYSTEM = "slack"
ANNOUNCED = "slack:announce:"
GAP_ROUNDS = 3


def log(msg: str) -> None:
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {MACHINE}: {msg}", file=sys.stderr, flush=True)


class Bridge:
    def __init__(self, config: SlackConfig, api: Slack, events: Events, store: MetricsStore, *,
                 people: People | None = None, clock: Callable[[], float] = time.time):
        self.config, self.api, self.events, self.store, self.clock = config, api, events, store, clock
        self.people = people if people is not None else People.load(config.people)
        self.lock = threading.Lock()
        self.me = self.my_name = self.team = ""
        self.waits = Waits()
        self.seen: set[str] = set()
        self.announced: dict[str, dict] = {}
        self.last_ok: float | None = None             # when the last good poll began
        self.since = 0.0                              # where the next poll reads from
        self.ready = False
        self.server: MetricsServer | None = None       # while it runs

    def identify(self) -> None:
        a = self.api.auth()
        self.me, self.my_name, self.team = str(a["user_id"]), str(a.get("user") or ""), str(a["team_id"])

    @property
    def watched(self) -> set[str]:
        return {self.me, *self.config.groups}

    def labels(self) -> dict[str, str]:
        return {"workspace": self.team}

    def actor(self, user: str | None, username: str | None, bot: bool = False) -> Actor:
        """The identity map's person when it lists the member id or the handle; else by handle."""
        for login in (user, username):
            if self.people.listed(SYSTEM, login):
                return self.people.actor(SYSTEM, login, bot)
        return self.people.actor(SYSTEM, username or user, bot)

    def owner(self) -> Actor:
        return self.actor(self.me, self.my_name)

    def queries(self) -> list[str]:
        if self.config.queries:
            return [q.replace("{me}", self.me) for q in self.config.queries]
        qs = [search.mention_query(self.me), *(search.group_query(g) for g in self.config.groups)]
        return qs + ([search.dm_query(self.me)] if self.config.dms else [])

    def _stored(self) -> tuple[dict[str, float], float]:
        """Each counter's last value in the store, and when the store last scraped the bridge."""
        base = {"machine": MACHINE, **self.labels()}
        written: dict[str, float] = {}
        try:
            last = self.store.latest(LEVEL, base)
            for key, (name, extra) in COUNTERS.items():
                if got := self.store.latest(name, {**base, **extra}):
                    written[key] = got[1]
        except Exception as e:  # noqa: BLE001 - measuring never stops the bridge
            log(f"metrics store not read ({type(e).__name__})")
            last = None
        return written, (last[0] if last else 0.0)

    def resume(self) -> None:
        """Read back what this bridge published before, and where to read Slack from."""
        evs, _ = self.events.after(0)
        own = [e for e in evs if str(e.get("id", "")).startswith("slack:")]
        mine = []
        for e in own:
            p = parse_id(e["id"])
            if p and p[0] == self.team:
                mine.append((event_time(e) or 0.0, RANK[p[1]], p[1], p[2], p[3], e.get("subject") or ""))
        waits = Waits()
        for t, _, type_, channel, ts, subj in sorted(mine):
            waits.fold(type_, channel, ts, subj, t)
        written, scraped = self._stored()
        waits.continue_from(written)
        last = max([scraped, *(m[0] for m in mine)])
        with self.lock:
            self.seen = {e["id"] for e in own}
            self.announced = {e.get("source"): e.get("data") or {} for e in own
                              if e["id"].startswith(ANNOUNCED) and e.get("type") == "orchestra.machine.started"}
            self.waits = waits
            self.last_ok = scraped or None
            self.since = last - self.config.overlap if last else self.clock() - self.config.lookback
            self.ready = True
        log(f"resumed: {len(mine)} of its events on the bus, {len(waits.waiting)} pings waiting; "
            f"reading from {iso(self.since)}")

    def announce(self) -> None:
        self.events.publish("orchestra.machine.started", {
            "mode": "continuous", "kind": "program", "package": "bridges", "listens_for": [],
            "log": os.environ.get("ORCHESTRA_MACHINE_LOG"), "system": SYSTEM, "workspace": self.team,
            "answered": self.config.answered, "every_s": self.config.every}, source=MACHINE)

    def _announce_actor(self, actor: Actor, when: str) -> None:
        """Announce ``actor`` unless their last announcement on the bus says the same."""
        about = actor.announcement(SYSTEM)
        if self.announced.get(actor.source) == about:
            return
        aid = f"{ANNOUNCED}{actor.source}:{hashlib.sha1(json.dumps(about, sort_keys=True).encode()).hexdigest()[:12]}:{when}"
        self.events.publish("orchestra.machine.started", about, source=actor.source, time=when, id=aid)
        self.seen.add(aid)
        self.announced[actor.source] = about

    def reannounce(self) -> None:
        """Everyone the identity map lists who was announced before, again when their name or team
        is not what their last announcement says."""
        now = iso(self.clock())
        for login in self.people.accounts(SYSTEM):
            actor = self.people.actor(SYSTEM, login)
            last = self.announced.get(actor.source)
            if last is not None and (last.get("name"), last.get("team")) != (actor.name, actor.team):
                self._announce_actor(actor, now)

    def _window(self, now: float) -> tuple[float, dict | None]:
        start, last = self.since, self.last_ok
        floor = now - self.config.max_catchup
        gap = None
        if last and now - last > GAP_ROUNDS * self.config.every:
            gap = {"workspace": self.team, "from": iso(last), "to": iso(now), "recovered": start >= floor}
        return max(start, floor), gap

    def _read(self, start: float) -> tuple[list[search.Message], list[search.Message], dict]:
        """Every read of one poll: the pings, the owner's messages, and the reactions on each ping
        that waits (or ``None`` for one whose message is gone)."""
        found: dict[tuple[str, str], search.Message] = {}
        for q in self.queries():
            for m in search.since(self.api, q, start, self.watched):
                found[(m.channel, m.ts)] = m
        pings = [m for m in found.values() if kind(m, self.me, self.config.groups, self.config.dms)]
        pings.sort(key=lambda m: (m.t, m.channel))
        own = []
        if "reply" in self.config.answered:
            own = [m for m in search.since(self.api, search.own_query(self.me), start, self.watched) if m.user == self.me]
        reactions: dict[tuple[str, str], list[dict] | None] = {}
        if "reaction" in self.config.answered:
            new = {(m.channel, m.ts) for m in pings if event_id(self.team, RECEIVED, m.channel, m.ts) not in self.seen}
            for key in sorted({*self.waits.waiting, *new}):
                try:
                    reactions[key] = self.api.reactions(*key)
                except SlackError as e:
                    if e.error != "message_not_found":
                        raise
                    reactions[key] = None
        return pings, own, reactions

    def _answer(self, key: tuple[str, str], at: float, when: str, data: dict) -> None:
        subj, t0 = self.waits.waiting[key]
        eid = event_id(self.team, ANSWERED, *key)
        owner = self.owner()
        self._announce_actor(owner, when)
        wait = round(max(at - t0, 0.0), 3)
        self.events.publish(ANSWERED, {"workspace": self.team, "channel": key[0], "ts": key[1], "wait_seconds": wait, **data},
                            source=owner.source, subject=subj, time=when, id=eid)
        self.seen.add(eid)
        self.waits.fold(ANSWERED, *key, subj, at)

    def _expire(self, key: tuple[str, str], at: float, reason: str) -> None:
        subj, t0 = self.waits.waiting[key]
        eid = event_id(self.team, EXPIRED, *key)
        self.events.publish(EXPIRED, {"workspace": self.team, "channel": key[0], "ts": key[1], "reason": reason,
                                      "waited_seconds": round(at - t0, 3)},
                            source=MACHINE, subject=subj, time=iso(at), id=eid)
        self.seen.add(eid)
        self.waits.fold(EXPIRED, *key, subj, at)

    def _receive(self, m: search.Message) -> bool:
        eid = event_id(self.team, RECEIVED, m.channel, m.ts)
        if eid in self.seen:
            return False
        actor = self.actor(m.user, m.username, m.bot)
        when = iso_ts(m.ts)
        subj = subject(self.team, m.channel, m.thread)
        self._announce_actor(actor, when)
        self.events.publish(RECEIVED, {"workspace": self.team, "channel": m.channel, "channel_type": m.channel_type,
                                       "ts": m.ts, "thread_ts": m.thread_ts, "user": m.user,
                                       "kind": kind(m, self.me, self.config.groups, self.config.dms)},
                            source=actor.source, subject=subj, time=when, id=eid)
        self.seen.add(eid)
        self.waits.fold(RECEIVED, m.channel, m.ts, subj, m.t)
        return True

    def _replies(self, own: list[search.Message]) -> None:
        """The reply rule: each of the owner's messages, in time order, answers the pings before it
        in its thread, or, when it is not in a thread, the pings before it in its channel that were
        not in one either."""
        for r in sorted(own, key=lambda m: (m.t, m.channel)):
            if r.thread_ts:
                keys = self.waits.in_thread(subject(self.team, r.channel, r.thread), r.t)
            else:
                keys = sorted((k for k, (s, t) in self.waits.waiting.items()
                               if k[0] == r.channel and s == subject(self.team, r.channel, k[1]) and t < r.t),
                              key=lambda k: float(k[1]))
            for key in keys:
                if self.waits.waiting[key][1] + self.config.expire > r.t:
                    self._answer(key, r.t, iso_ts(r.ts), {"rule": "reply", "reply_ts": r.ts})

    def _expiries(self, now: float) -> None:
        for key, (_, t0) in sorted(self.waits.waiting.items(), key=lambda kv: kv[1][1]):
            if t0 + self.config.expire <= now:
                self._expire(key, t0 + self.config.expire, "expired")

    def _reactions(self, reactions: dict, now: float, not_before: float | None) -> None:
        names = set(self.config.reactions)
        for key, got in sorted(reactions.items(), key=lambda kv: float(kv[0][1])):
            if key not in self.waits.waiting:
                continue
            if got is None:
                self._expire(key, now, "deleted")
                continue
            hit = next((r["name"] for r in got if r.get("name", "").split("::")[0] in names and self.me in (r.get("users") or [])),
                       None)
            if hit is None:
                continue
            subj, t0 = self.waits.waiting[key]
            for k in [*self.waits.in_thread(subj, t0), key]:
                since = max(self.waits.waiting[k][1], not_before or 0.0)
                self._answer(k, now, iso(now), {"rule": "reaction", "reaction": hit.split("::")[0], "observed": True,
                                                "not_before": iso(since)})

    def poll(self) -> int:
        """Read Slack once and publish what is new; how many pings were received."""
        now = self.clock()
        start, gap = self._window(now)
        pings, own, reactions = self._read(start)
        with self.lock:
            if gap:
                self.events.publish("bridge.gap", gap, source=MACHINE)
                log(f"gap of {round(now - self.last_ok)} s since the last good poll"
                    + ("" if gap["recovered"] else f"; only the last {round(self.config.max_catchup)} s were read"))
            new = sum(self._receive(m) for m in pings)
            self._replies(own)
            self._expiries(now)
            self._reactions(reactions, now, self.last_ok)
            self.last_ok = now
            self.since = now - self.config.overlap
        return new

    def render(self) -> str | None:
        """What ``/metrics`` serves; None until the bridge has read its past back."""
        with self.lock:
            return self.waits.exposition(self.clock(), self.labels()) if self.ready else None

    def round(self) -> None:
        try:
            n = self.poll()
            log(f"{n} new pings; {len(self.waits.waiting)} waiting; {self.api.requests} requests so far")
        except SlackError as e:
            log(f"poll failed, tried again next round: {e}")

    def run(self, stop: threading.Event, *, once: bool = False) -> None:
        self.server = server = MetricsServer(self.render, self.config.host, self.config.port).start()
        log(f"serving {server.url}")
        try:
            while not stop.is_set():
                try:
                    self.identify()
                    break
                except SlackError as e:
                    log(f"auth.test failed, tried again next round: {e}")
                    if once:
                        return
                    stop.wait(self.config.every)
            if stop.is_set():
                return
            self.resume()
            self.announce()
            self.reannounce()
            while not stop.is_set():
                self.round()
                if once:
                    return
                stop.wait(self.config.every)
        finally:
            server.stop()
