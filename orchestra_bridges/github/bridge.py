"""The GitHub issues bridge: polls repositories and publishes what people did to their issues on
the project's Orchestra bus, as the people who did it.

Each event's **source** is the actor (``identity.py``: ``person.<login>``, or the identity map's
``person.<key>`` with a name and a team), its **subject** the issue (``microsoft/vscode#1234``), its
**time** and **id** GitHub's (``translate.py``). Each actor is announced once, before their first
event, with ``orchestra.machine.started`` (kind person, or program for a bot; package ``bridges``;
their name and team when the map gives them), stamped with the time of that event. Whenever what
it would announce for someone differs from their last announcement on the bus (their name or team
changed in the map), they are announced again: before their next event, and on start for everyone
the map lists who was announced before, even if they do nothing, so the Machines page shows the
team they are in now.

**It keeps no state of its own.** On start it reads its own events back from the bus (ids starting
``github:``): what it already published is never published again, the first-response counts are
rebuilt from them and continue from the values last written (a counter never goes down), and it
reads GitHub again from the later of its last event and its last
measurement (the last poll), less ``overlap``; never further back than ``max_catchup``. A poll that
succeeds more than three rounds after the last good one (the laptop slept, the bridge was stopped,
GitHub or the network was down for a while) closes a **gap**, recorded on the bus once, when the
poll has succeeded, as ``bridge.gap`` {repository, from, to, recovered}: from the last good poll to
this one; ``recovered`` is false when the gap was longer than ``max_catchup``, so some of it was
not read. Failed polls in between publish nothing.

An event is published as soon as it is read, but counted only once it is older than ``overlap``:
each poll re-reads that much, so an event GitHub shows late (a comment stamped before a close
that was already read) is still counted in time order, as a restart's rebuild counts it.

The bridge itself is the machine ``bridges.github``; it announces itself when it starts and its
measurements carry ``machine="bridges.github"``."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import threading
import time
from collections import defaultdict
from collections.abc import Callable

from orchestra.events import Events, event_time
from orchestra.metrics import MetricsStore

from ..config import GitHubConfig
from ..identity import Actor, People
from . import poll
from .api import GitHub, GitHubError
from .measures import Measures
from .responses import Responses
from .translate import RANK, TYPES, prefix

MACHINE = "bridges.github"
SYSTEM = "github"
ANNOUNCED = "github:announce:"
GAP_ROUNDS = 3


def log(msg: str) -> None:
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {MACHINE}: {msg}", file=sys.stderr, flush=True)


class Bridge:
    def __init__(self, config: GitHubConfig, api: GitHub, events: Events, store: MetricsStore, *,
                 people: People | None = None, clock: Callable[[], float] = time.time):
        self.config, self.api, self.events, self.clock = config, api, events, clock
        self.people = people if people is not None else People.load(config.people)
        self.measures = Measures(store, MACHINE)
        self.responses = {r: Responses(config.bots_answer) for r in config.repos}
        self.seen: set[str] = set()
        self.last_ok: dict[str, float] = {}            # repo -> when its last poll began
        self.since: dict[str, float] = {}              # repo -> where its next poll reads from
        self.pending: dict[str, list[tuple]] = {r: [] for r in config.repos}   # repo -> (t, rank, type, subject, source) not counted yet
        self.announced: dict[str, dict] = {}           # source -> its last announcement on the bus

    def resume(self) -> None:
        """Read back what this bridge published before, and where to read GitHub from."""
        evs, _ = self.events.after(0)
        own = [e for e in evs if str(e.get("id", "")).startswith("github:")]
        self.seen = {e["id"] for e in own}
        self.announced = {e.get("source"): e.get("data") or {} for e in own
                          if e["id"].startswith(ANNOUNCED) and e.get("type") == "orchestra.machine.started"}
        self.responses = {r: Responses(self.config.bots_answer) for r in self.config.repos}
        latest: dict[str, float] = defaultdict(float)
        for repo in self.config.repos:
            mine = [e for e in own if e["id"].startswith(prefix(repo)) and e.get("type") in TYPES]
            self.pending[repo] = [(event_time(e) or 0.0, RANK[e["type"]], e["type"], e.get("subject") or "", e.get("source") or "")
                                  for e in mine]
            latest[repo] = max((p[0] for p in self.pending[repo]), default=0.0)
            self._count(repo, self.clock(), write=False)
            self.measures.resume(repo)
            self.responses[repo].continue_from(self.measures.written[repo])
            last = max(latest[repo], self.measures.watermark[repo])
            if self.measures.watermark[repo]:
                self.last_ok[repo] = self.measures.watermark[repo]
            if last:
                self.since[repo] = last - self.config.overlap
            else:
                self.since[repo] = self.clock() - self.config.lookback
        log(f"resumed: {len(own)} of its events on the bus; reading "
            + ", ".join(f"{r} from {poll.iso(self.since[r])}" for r in self.config.repos))

    def announce(self) -> None:
        self.events.publish("orchestra.machine.started", {
            "mode": "continuous", "kind": "program", "package": "bridges", "listens_for": [],
            "log": os.environ.get("ORCHESTRA_MACHINE_LOG"), "system": SYSTEM, "repos": self.config.repos,
            "every_s": self.config.every}, source=MACHINE)

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
        """Everyone the identity map lists who was announced before, announced again when their
        name or team is not what their last announcement says (the map changed while the bridge
        was stopped)."""
        now = poll.iso(self.clock())
        for login in self.people.accounts(SYSTEM):
            actor = self.people.actor(SYSTEM, login)
            last = self.announced.get(actor.source)
            if last is not None and (last.get("name"), last.get("team")) != (actor.name, actor.team):
                self._announce_actor(actor, now)

    def _window(self, repo: str, now: float) -> tuple[float, dict | None]:
        """Where this poll reads from, and the gap it closes when the last good poll was too long
        ago (published only once the poll succeeds)."""
        start = self.since[repo]
        last = self.last_ok.get(repo)
        floor = now - self.config.max_catchup
        gap = None
        if last and now - last > GAP_ROUNDS * self.config.every:
            gap = {"repository": repo, "from": poll.iso(last), "to": poll.iso(now), "recovered": start >= floor}
        return max(start, floor), gap

    def _gap(self, gap: dict, seconds: float) -> None:
        self.events.publish("bridge.gap", gap, source=MACHINE)
        log(f"{gap['repository']}: gap of {round(seconds)} s since the last good poll"
            + ("" if gap["recovered"] else f"; only the last {round(self.config.max_catchup)} s were read"))

    def poll(self, repo: str) -> int:
        """Read ``repo`` once and publish what is new; how many events."""
        now = self.clock()
        start, gap = self._window(repo, now)
        found = poll.since(self.api, repo, start)
        if gap:
            self._gap(gap, now - self.last_ok[repo])
        new = [s for s in found if s.id not in self.seen]
        responses = self.responses[repo]
        for s in new:
            actor = self.people.actor(SYSTEM, s.login, s.bot)
            self._announce_actor(actor, s.time)
            self.events.publish(s.type, s.data, source=actor.source, subject=s.subject, time=s.time, id=s.id)
            self.seen.add(s.id)
            self.pending[repo].append((s.ts, RANK[s.type], s.type, s.subject, actor.source))
        self._count(repo, now)
        self.measures.write(repo, responses, now)
        self.last_ok[repo] = now
        self.since[repo] = now - self.config.overlap
        return len(new)

    def _count(self, repo: str, now: float, *, write: bool = True) -> None:
        """Count the pending events older than ``overlap``, in time order (later ones wait: a poll
        may still read an earlier one)."""
        ready = sorted(p for p in self.pending[repo] if p[0] <= now - self.config.overlap)
        self.pending[repo] = [p for p in self.pending[repo] if p[0] > now - self.config.overlap]
        for t, _, type_, subject, source in ready:
            if self.responses[repo].fold(type_, subject, source, t) and write:
                self.measures.write(repo, self.responses[repo], t)

    def round(self) -> None:
        for repo in self.config.repos:
            try:
                n = self.poll(repo)
                r = self.responses[repo]
                log(f"{repo}: {n} new events; {len(r.waiting)} issues unanswered; "
                    f"{self.api.requests} requests so far, {self.api.rate_remaining} left this hour")
            except GitHubError as e:
                log(f"{repo}: poll failed, tried again next round: {e}")

    def run(self, stop: threading.Event, *, once: bool = False) -> None:
        self.resume()
        self.announce()
        self.reannounce()
        while not stop.is_set():
            self.round()
            if once:
                return
            stop.wait(self.config.every)
