"""The GitHub pull-request bridge: follows pull requests to resolution and publishes what happens on
them on the project's Orchestra bus, as the people (and apps) who did it. It only reads.

**Which pull requests.** Of each configured repository: those whose branch matches one of
``branches`` (globs) or that carry one of ``labels``; with neither, all of them. Once followed, a
pull request is followed until it is merged or closed, whatever its labels become. A closed one is
taken up only if it was opened inside the window being read, so a first start does not publish a
repository's history. An open one is taken up whenever it is first seen: its ``github.pr.opened``
carries its own (perhaps old) time.

**What it publishes** (``translate.py``): opened, pushed, review requested and withdrawn, review
submitted (with its state and how many line comments it holds), each line comment (file, line,
side), conversation comments, labels added and removed, checks and statuses completed on the head,
merged, closed (unmerged) and reopened. The source is whoever acted, the subject the pull request
(``owner/repo#12``), the time and id GitHub's. Never text.

**Each poll first reads, then publishes**, so a poll that fails part way publishes nothing.

**It keeps no state of its own.** On start it reads its own events back from the bus (ids starting
``github:<repo>:pr:``): what it published is never published again, the pull requests it followed
are followed still, and the heads it published (in ``opened`` and ``pushed``) say whether a head is
new. It reads GitHub again from the later of its last event and its last good poll (the value of
``github_prs_bridge_last_poll_timestamp_seconds`` that the instrument stored), less ``overlap``;
never further back than ``max_catchup``; with neither, from ``lookback`` ago. A poll that succeeds
more than three rounds after the last good one records the gap once, as ``bridge.gap``
{repository, from, to, recovered}, and the events missed are published with their own times.

**What waits on whom** (``levels.py``) is read from GitHub each round, not rebuilt from events, and
served on ``/metrics``; the plugin declares the scrape that stores it (the instrument
``bridges.github_prs``). The bridge itself is the machine ``bridges.github_prs``: it announces
itself on start and publishes ``bridge.gap`` under that source."""

from __future__ import annotations

import os
import sys
import threading
import time
from collections import defaultdict
from collections.abc import Callable

from orchestra.events import Events, event_time
from orchestra.metrics import MetricsStore

from ..announce import Announcer
from ..config import GitHubPRsConfig
from ..github.api import GitHub, GitHubError
from ..identity import People
from ..serve import MetricsServer
from . import poll
from .levels import LAST_POLL, PullState, levels, render
from .translate import (
    RANK,
    Pull,
    Seen,
    check_run,
    commented,
    from_issue_event,
    opened,
    opening_head,
    parse_time,
    prefix,
    pull,
    pushed,
    reviewed,
    status,
    who,
)

MACHINE = "bridges.github_prs"
INSTRUMENT = "bridges.github_prs"        # the scrape the plugin declares (plugin.py)
SYSTEM = "github"
GAP_ROUNDS = 3
MERGE_CLOSE_S = 5.0                      # a close this near a merge is the merge's own


def log(msg: str) -> None:
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {MACHINE}: {msg}", file=sys.stderr, flush=True)


class Bridge:
    def __init__(self, config: GitHubPRsConfig, api: GitHub, events: Events, store: MetricsStore, *,
                 people: People | None = None, clock: Callable[[], float] = time.time):
        self.config, self.api, self.events, self.store, self.clock = config, api, events, store, clock
        self.people = people if people is not None else People.load(config.people)
        self.announcer = Announcer(events, SYSTEM, self.people)
        self.seen: set[str] = set()
        self.followed: dict[str, set[int]] = {r: set() for r in config.repos}
        self.heads: dict[str, dict[int, list[str]]] = {r: defaultdict(list) for r in config.repos}
        self.last_ok: dict[str, float] = {}            # repo -> when its last good poll began
        self.since: dict[str, float] = {}              # repo -> where its next poll reads from
        self.measured: dict[str, tuple[dict[str, float], float]] = {}   # repo -> (levels, when measured)
        self.lock = threading.Lock()
        self.server: MetricsServer | None = None

    def _stored_last_poll(self, repo: str) -> float:
        try:
            got = self.store.latest(LAST_POLL, {"instrument": INSTRUMENT, "repo": repo})
        except Exception as e:  # noqa: BLE001 - the store never stops the bridge
            log(f"metrics store not read ({type(e).__name__})")
            return 0.0
        return got[1] if got else 0.0

    def resume(self) -> None:
        """Read back what this bridge published before, and where to read GitHub from."""
        evs, _ = self.events.after(0)
        own = [e for e in evs if str(e.get("id", "")).startswith("github:")]
        self.announcer.from_events(own)
        now = self.clock()
        for repo in self.config.repos:
            mine = sorted((e for e in own if e["id"].startswith(prefix(repo))), key=lambda e: event_time(e) or 0.0)
            self.seen |= {e["id"] for e in mine}
            for e in mine:
                n = (e.get("data") or {}).get("pull")
                if e.get("type") == "github.pr.opened" and isinstance(n, int):
                    self.followed[repo].add(n)
                if e.get("type") in ("github.pr.opened", "github.pr.pushed") and isinstance(n, int):
                    self.heads[repo][n].append((e.get("data") or {}).get("head"))
            latest = max((event_time(e) or 0.0 for e in mine), default=0.0)
            last_poll = self._stored_last_poll(repo)
            if last_poll:
                self.last_ok[repo] = last_poll
            last = max(latest, last_poll)
            self.since[repo] = last - self.config.overlap if last else now - self.config.lookback
        log(f"resumed: {len(self.seen)} of its events on the bus; reading "
            + ", ".join(f"{r} from {poll.iso(self.since[r])}" for r in self.config.repos))

    def announce(self) -> None:
        self.events.publish("orchestra.machine.started", {
            "mode": "continuous", "kind": "program", "package": "bridges", "listens_for": [],
            "log": os.environ.get("ORCHESTRA_MACHINE_LOG"), "system": SYSTEM, "repos": self.config.repos,
            "every_s": self.config.every}, source=MACHINE)

    def _window(self, repo: str, now: float) -> tuple[float, dict | None]:
        start = self.since[repo]
        last = self.last_ok.get(repo)
        floor = now - self.config.max_catchup
        gap = None
        if last and now - last > GAP_ROUNDS * self.config.every:
            gap = {"repository": repo, "from": poll.iso(last), "to": poll.iso(now), "recovered": start >= floor}
        return max(start, floor), gap

    def _to_follow(self, repo: str, start: float) -> list[Pull]:
        followed = self.followed[repo]
        out = {p.number: p for p in (pull(repo, x) for x in poll.open_pulls(self.api, repo))
               if p.number in followed or self.config.follows(p.branch, list(p.labels))}
        for p in (pull(repo, x) for x in poll.closed_since(self.api, repo, start)):
            if p.number not in out and (p.number in followed or (
                    self.config.follows(p.branch, list(p.labels)) and parse_time(p.created_at) >= start)):
                out[p.number] = p
        return sorted(out.values(), key=lambda p: p.number)

    def _pushed(self, p: Pull, heads: list[str], tl: list[dict], start: float, now: float) -> Seen:
        """The new head, with who pushed it and when, as well as GitHub tells: a force push is
        recorded with its actor; an ordinary push is not, so the head commit's GitHub committer (or
        author) stands in, else the pull request's author, at the commit's date when that falls in the
        window, else now."""
        previous = heads[-1]
        force = [e for e in tl if e.get("event") == "head_ref_force_pushed" and e.get("commit_id") == p.head]
        if force:
            login, bot = who(force[-1].get("actor"))
            return pushed(p, force[-1]["created_at"], login, bot, previous, forced=True, by="force_push")
        c = poll.commit(self.api, p.repo, p.head)
        committer, author = who(c.get("committer")), who(c.get("author"))
        if committer[0] and committer[0] != "web-flow":
            (login, bot), by = committer, "committer"
        elif author[0]:
            (login, bot), by = author, "author"
        else:
            (login, bot), by = (p.login, p.bot), "pull_author"
        date = ((c.get("commit") or {}).get("committer") or {}).get("date")
        at = date if date and start <= parse_time(date) <= now else poll.iso(now)
        return pushed(p, at, login, bot, previous, forced=False, by=by)

    def _read(self, p: Pull, tl: list[dict], runs: list[dict], statuses: list[dict], start: float,
              now: float) -> list[Seen]:
        """What is new on one pull request, read but not yet published."""
        out: list[Seen] = []
        heads = self.heads[p.repo][p.number]
        first = opened(p, opening_head(p, tl))
        if first.id not in self.seen:
            out.append(first)
            heads = [first.data["head"]]
        merges = [parse_time(e["created_at"]) for e in tl if e.get("event") == "merged" and e.get("created_at")]
        lines: list[dict] | None = None                      # read once, when there is a new review
        for e in tl:
            kind = e.get("event")
            if kind == "reviewed":
                rid = f"{prefix(p.repo, p.number)}review:{e.get('id')}"
                if rid in self.seen or not e.get("submitted_at") or parse_time(e["submitted_at"]) < start:
                    continue
                if lines is None:
                    lines = poll.line_comments(self.api, p.repo, p.number)
                out += reviewed(p, e, [c for c in lines if c.get("pull_request_review_id") == e.get("id")])
                continue
            s = commented(p, e) if kind == "commented" else from_issue_event(p, e)
            if s and s.type == "github.pr.closed" and any(abs(s.ts - m) <= MERGE_CLOSE_S for m in merges):
                continue
            if s and s.id not in self.seen and s.ts >= start:
                out.append(s)
        if heads and p.head and p.head not in heads and f"{prefix(p.repo, p.number)}push:{p.head}" not in self.seen:
            out.append(self._pushed(p, heads, tl, start, now))
        checks = [check_run(p, r) for r in runs] + [status(p, st, p.head) for st in statuses]
        out += [s for s in checks if s and s.id not in self.seen and s.ts >= start]
        return out

    def poll(self, repo: str) -> int:
        """Read ``repo`` once, then publish what is new; how many events."""
        now = self.clock()
        start, gap = self._window(repo, now)
        found: list[Seen] = []
        states: list[PullState] = []
        follow = self._to_follow(repo, start)
        for p in follow:
            tl = poll.timeline(self.api, repo, p.number)
            live = self.config.checks and p.head
            runs = poll.check_runs(self.api, repo, p.head) if live else []
            sts = poll.statuses(self.api, repo, p.head) if live else []
            found += self._read(p, tl, runs, sts, start, now)
            if p.state == "open":
                states.append(PullState.read(p, tl, runs, sts))
        if gap:
            self.events.publish("bridge.gap", gap, source=MACHINE)
            log(f"{repo}: gap of {round(now - self.last_ok[repo])} s since the last good poll"
                + ("" if gap["recovered"] else f"; only the last {round(self.config.max_catchup)} s were read"))
        unique = {s.id: s for s in found}
        for s in sorted(unique.values(), key=lambda s: (s.ts, RANK[s.type], s.id)):
            actor = self.people.actor(SYSTEM, s.login, s.bot)
            self.announcer.actor(actor, s.time)
            self.events.publish(s.type, s.data, source=actor.source, subject=s.subject, time=s.time, id=s.id)
            self.seen.add(s.id)
            if s.type in ("github.pr.opened", "github.pr.pushed"):
                self.heads[repo][s.pull.number].append(s.data["head"])
        for p in follow:
            self.followed[repo].add(p.number)
            if not self.heads[repo][p.number]:               # its opened event is no longer on the bus
                self.heads[repo][p.number].append(p.head)
        with self.lock:
            self.measured[repo] = (levels(states, self.config.label_results, now), now)
        self.last_ok[repo] = now
        self.since[repo] = now - self.config.overlap
        return len(unique)

    def render(self) -> str | None:
        """``/metrics``: None (503) until a poll has measured something."""
        with self.lock:
            return render(dict(self.measured)) if self.measured else None

    def round(self) -> None:
        for repo in self.config.repos:
            try:
                n = self.poll(repo)
                lv = self.measured[repo][0]
                log(f"{repo}: {n} new events; {int(lv['github_prs_open'])} open followed, "
                    f"{int(lv['github_prs_awaiting_review'])} awaiting review, "
                    f"{int(lv['github_prs_awaiting_changes'])} awaiting changes; "
                    f"{self.api.requests} requests so far, {self.api.rate_remaining} left this hour")
            except GitHubError as e:
                log(f"{repo}: poll failed, tried again next round: {e}")

    def run(self, stop: threading.Event, *, once: bool = False) -> None:
        self.resume()
        self.announce()
        self.announcer.reannounce(poll.iso(self.clock()))
        self.server = server = None if once else MetricsServer(self.render, self.config.host, self.config.port).start()
        if server:
            log(f"serving {server.url}")
        try:
            while not stop.is_set():
                self.round()
                if once:
                    return
                stop.wait(self.config.every)
        finally:
            if server:
                server.stop()
