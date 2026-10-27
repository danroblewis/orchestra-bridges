"""The GitHub issues bridge: polls repositories and publishes what people did to their issues on
the project's Orchestra bus, as the people who did it.

Each event's **source** is the actor (``identity.py``: ``person.<login>``, or the identity map's
``person.<key>`` with a name and a team), its **subject** the issue (``microsoft/vscode#1234``), its
**time** and **id** GitHub's (``translate.py``). Each actor is announced once, before their first
event, with ``orchestra.machine.started`` (kind person, or program for a bot; package ``bridges``;
their name and team when the map gives them), stamped with the time of that event; a changed name
or team is announced again.

**It keeps no state of its own.** On start it reads its own events back from the bus (ids starting
``github:``): what it already published is never published again, the first-response counts are
rebuilt from them, and it reads GitHub again from the later of its last event and its last
measurement (the last poll), less ``overlap``; never further back than ``max_catchup``. A poll that
starts more than three rounds after the last one (the laptop slept, the bridge was stopped) is a
**gap**, recorded on the bus as ``bridge.gap`` {repository, from, to, recovered}: ``recovered`` is
false when the gap was longer than ``max_catchup``, so some of it was not read.

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

    def resume(self) -> None:
        """Read back what this bridge published before, and where to read GitHub from."""
        evs, _ = self.events.after(0)
        own = [e for e in evs if str(e.get("id", "")).startswith("github:")]
        self.seen = {e["id"] for e in own}
        self.responses = {r: Responses(self.config.bots_answer) for r in self.config.repos}
        latest: dict[str, float] = defaultdict(float)
        for repo in self.config.repos:
            mine = [e for e in own if e["id"].startswith(prefix(repo)) and e.get("type") in TYPES]
            mine.sort(key=lambda e: (event_time(e) or 0.0, RANK[e["type"]]))
            for e in mine:
                t = event_time(e) or 0.0
                latest[repo] = max(latest[repo], t)
                self.responses[repo].fold(e["type"], e.get("subject") or "", e.get("source") or "", t)
            self.measures.resume(repo)
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
        about = actor.announcement(SYSTEM)
        aid = f"{ANNOUNCED}{actor.source}:{hashlib.sha1(json.dumps(about, sort_keys=True).encode()).hexdigest()[:12]}"
        if aid not in self.seen:
            self.events.publish("orchestra.machine.started", about, source=actor.source, time=when, id=aid)
            self.seen.add(aid)

    def _window(self, repo: str, now: float) -> float:
        """Where this poll reads from; records a gap when the last poll was too long ago."""
        start = self.since[repo]
        last = self.last_ok.get(repo)
        floor = now - self.config.max_catchup
        if last and now - last > GAP_ROUNDS * self.config.every:
            recovered = start >= floor
            self.events.publish("bridge.gap", {"repository": repo, "from": poll.iso(last), "to": poll.iso(now),
                                               "recovered": recovered}, source=MACHINE)
            log(f"{repo}: gap of {round(now - last)} s since the last poll"
                + ("" if recovered else f"; only the last {round(self.config.max_catchup)} s are read"))
        return max(start, floor)

    def poll(self, repo: str) -> int:
        """Read ``repo`` once and publish what is new; how many events."""
        now = self.clock()
        start = self._window(repo, now)
        found = poll.since(self.api, repo, start)
        new = [s for s in found if s.id not in self.seen]
        responses = self.responses[repo]
        for s in new:
            actor = self.people.actor(SYSTEM, s.login, s.bot)
            self._announce_actor(actor, s.time)
            self.events.publish(s.type, s.data, source=actor.source, subject=s.subject, time=s.time, id=s.id)
            self.seen.add(s.id)
            if responses.fold(s.type, s.subject, actor.source, s.ts):
                self.measures.write(repo, responses, s.ts)
        self.measures.write(repo, responses, now)
        self.last_ok[repo] = now
        self.since[repo] = now - self.config.overlap
        return len(new)

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
        while not stop.is_set():
            self.round()
            if once:
                return
            stop.wait(self.config.every)
