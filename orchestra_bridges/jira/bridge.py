"""The Jira bridge: what people do on the issues it follows, published on the project's Orchestra bus
as the people who did it. It only reads.

**What it follows**: the issues of ``projects``, or those a ``jql`` query matches, on one Jira site
(Data Center, or Cloud). A second site (the FedRAMP instance, say) is a second bridge,
``[bridges.jira_<name>]``, with its own machine, instrument and token.

**What it publishes** (``translate.py``): created, assigned, transitioned, updated (sprint,
priority, labels, links, due date) and comment added; the source whoever acted, the subject the
issue's key, the time and id Jira's. Never text: summaries and descriptions are never asked for,
and comment bodies are dropped as they are read (``records.py``).

**Each poll first reads, then publishes**, so a poll that fails part way publishes nothing.

**It keeps no state of its own.** On start it reads its own events back from the bus (ids starting
``jira:<site>:issue:``), so nothing is published twice, and reads Jira again from the later of its
last event and its last good poll (``jira_bridge_last_poll_timestamp_seconds``, which its
instrument stored), less ``overlap``; never further back than ``max_catchup``; with neither, from
``lookback`` ago. A poll that succeeds more than three rounds after the last good one records the
gap once, as ``bridge.gap`` {site, from, to, recovered}, and the events missed are published with
their own times.

**Levels** (``levels.py``) are read from Jira each round and served on ``/metrics``; the plugin
declares the scrape that stores them (the instrument ``bridges.<section>``). The bridge itself is
the machine ``bridges.<section>`` (``bridges.jira``): it announces itself on start and publishes
``bridge.gap`` and Jira's own changes (a change with no author) under that source."""

from __future__ import annotations

import dataclasses
import os
import sys
import threading
import time
from collections import Counter
from collections.abc import Callable
from datetime import UTC, datetime

from orchestra.events import Events, event_time
from orchestra.metrics import MetricsStore

from ..announce import Announcer
from ..config import JiraConfig
from ..identity import Actor, People
from ..serve import MetricsServer
from . import poll
from .api import Jira, JiraError
from .levels import LAST_POLL, measure, render
from .records import User
from .translate import RANK, Seen, events, prefix

SYSTEM = "jira"                       # the identity map's key for Jira accounts, on every site
GAP_ROUNDS = 3


def iso(t: float) -> str:
    return datetime.fromtimestamp(t, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class Bridge:
    def __init__(self, config: JiraConfig, api: Jira, events: Events, store: MetricsStore, *,
                 people: People | None = None, clock: Callable[[], float] = time.time):
        self.config, self.api, self.events, self.store, self.clock = config, api, events, store, clock
        self.machine = self.instrument = f"bridges.{config.name}"
        self.people = people if people is not None else People.load(config.people)
        self.announcer = Announcer(events, SYSTEM, self.people)
        self.site: poll.Site | None = None
        self.seen: set[str] = set()
        self.last_ok: float | None = None
        self.since = 0.0
        self.done: Counter = Counter()
        self.done_at: float | None = None
        self.levels: dict | None = None
        self.lock = threading.Lock()
        self.server: MetricsServer | None = None

    def log(self, msg: str) -> None:
        print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {self.machine}: {msg}", file=sys.stderr, flush=True)

    def _login(self, u: User) -> str | None:
        """The account the identity map knows: Data Center's user key when the map lists that, else
        the username (or Cloud's account id)."""
        if u.key and u.key != u.login and self.people.listed(SYSTEM, u.key) and not self.people.listed(SYSTEM, u.login):
            return u.key
        return u.login

    def actor(self, u: User | None) -> Actor | None:
        """The machine behind a Jira user, named from the map, else by Jira's display name; None for
        Jira itself."""
        if u is None or not u.login:
            return None
        a = self.people.actor(SYSTEM, self._login(u), u.bot, namespaced=True)
        return a if a.name or not u.name else dataclasses.replace(a, name=u.name)

    def machine_of(self, login: str) -> str:
        return self.people.actor(SYSTEM, login, namespaced=True).source

    def is_bot(self, login: str | None, says_bot: bool) -> bool:
        return self.people.actor(SYSTEM, login, says_bot, namespaced=True).bot if login else says_bot

    def _stored_last_poll(self) -> float:
        try:
            got = self.store.latest(LAST_POLL, {"instrument": self.instrument})
        except Exception as e:  # noqa: BLE001 - the store never stops the bridge
            self.log(f"metrics store not read ({type(e).__name__})")
            return 0.0
        return got[1] if got else 0.0

    def resume(self) -> None:
        """Read back what this bridge published before, and where to read Jira from."""
        evs, _ = self.events.after(0)
        self.announcer.from_events(e for e in evs if str(e.get("id", "")).startswith(f"{SYSTEM}:"))
        mine = [e for e in evs if str(e.get("id", "")).startswith(prefix(self.config.site))]
        self.seen = {e["id"] for e in mine}
        latest = max((event_time(e) or 0.0 for e in mine), default=0.0)
        last_poll = self._stored_last_poll()
        self.last_ok = last_poll or None
        last = max(latest, last_poll)
        self.since = last - self.config.overlap if last else self.clock() - self.config.lookback
        self.log(f"resumed: {len(self.seen)} of its events on the bus; reading from {iso(self.since)}")

    def announce(self) -> None:
        self.events.publish("orchestra.machine.started", {
            "mode": "continuous", "kind": "program", "package": "bridges", "listens_for": [],
            "log": os.environ.get("ORCHESTRA_MACHINE_LOG"), "system": SYSTEM, "site": self.config.site,
            "deployment": self.config.deployment, "projects": self.config.projects, "jql": bool(self.config.jql),
            "every_s": self.config.every}, source=self.machine)

    def _window(self, now: float) -> tuple[float, dict | None]:
        floor = now - self.config.max_catchup
        gap = None
        if self.last_ok and now - self.last_ok > GAP_ROUNDS * self.config.every:
            gap = {"site": self.config.site, "from": iso(self.last_ok), "to": iso(now), "recovered": self.since >= floor}
        return max(self.since, floor), gap

    def poll(self) -> int:
        """Read Jira once, then publish what is new; how many events."""
        now = self.clock()
        start, gap = self._window(now)
        scope = self.config.scope()
        if self.site is None:
            self.site = poll.site(self.api, self.config.sprint_field)
        site = self.site
        issues = poll.updated(self.api, scope, now - start, site)
        if any(c.new not in site.statuses for i in issues for h in i.histories for c in h.changes if c.field == "status"):
            site.statuses = poll.statuses(self.api)                      # a status added since
        open_ = poll.open_issues(self.api, scope)
        done_due = self.config.done_every > 0 and (self.done_at is None or now - self.done_at >= self.config.done_every)
        done = poll.done_count(self.api, scope) if done_due else self.done
        mine = poll.mine(self.api, scope)
        found = [s for i in issues for s in events(self.config.site, i, site.statuses, self.machine_of)
                 if s.id not in self.seen and s.ts >= start]
        if gap:
            self.events.publish("bridge.gap", gap, source=self.machine)
            self.log(f"gap of {round(now - self.last_ok)} s since the last good poll"
                     + ("" if gap["recovered"] else f"; only the last {round(self.config.max_catchup)} s were read"))
        unique = {s.id: s for s in found}
        for s in sorted(unique.values(), key=lambda s: (s.ts, RANK[s.type], s.id)):
            self._publish(s)
        if done_due:
            self.done, self.done_at = done, now
        levels = measure(self.config.projects, open_, self.done, mine, site.me.login, self.is_bot,
                         self.config.bots_ask, now)
        with self.lock:
            self.levels = levels
        self.last_ok = now
        self.since = now - self.config.overlap
        return len(unique)

    def _publish(self, s: Seen) -> None:
        actor = self.actor(s.actor)
        if actor:
            self.announcer.actor(actor, s.time)
        self.events.publish(s.type, s.data, source=actor.source if actor else self.machine, subject=s.subject,
                            time=s.time, id=s.id)
        self.seen.add(s.id)

    def render(self) -> str | None:
        """``/metrics``: None (503) until a poll has measured something."""
        with self.lock:
            return render(self.levels, self.last_ok or 0.0) if self.levels is not None else None

    def round(self) -> None:
        try:
            n = self.poll()
            lv = self.levels or {}
            self.log(f"{n} new events; {sum(lv.get('jira_issues_mine_not_started', {}).values())} of the owner's "
                     f"not started, {sum(lv.get('jira_comments_unanswered', {}).values())} with a comment unanswered; "
                     f"{self.api.requests} requests so far")
        except JiraError as e:
            self.log(f"poll failed, tried again next round: {e}")

    def run(self, stop: threading.Event, *, once: bool = False) -> None:
        self.resume()
        self.announce()
        self.announcer.reannounce(iso(self.clock()),
                                  lambda login: self.people.actor(SYSTEM, login, namespaced=True))
        self.server = server = None if once else MetricsServer(self.render, self.config.host, self.config.port).start()
        if server:
            self.log(f"serving {server.url}")
        try:
            while not stop.is_set():
                self.round()
                if once:
                    return
                stop.wait(self.config.every)
        finally:
            if server:
                server.stop()
