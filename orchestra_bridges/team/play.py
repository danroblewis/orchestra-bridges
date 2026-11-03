"""The scripted team's day played against Jira and Discord (either may be absent), round by round,
and the close of what earlier days created. Each act is printed as it is done (with ``dry_run``:
as it would be, nothing written, the ledger untouched; reads still happen: the people, the channels'
names, a ticket's status). An act that fails is printed and noted, and the day goes on."""

from __future__ import annotations

import copy
import time
from collections.abc import Callable

from .day import PREFIX, Act, Day
from .discord import Discord
from .jira import Jira
from .ledger import Ledger, now
from .wire import WireError

TEAM_LABEL = "orchestra-live-team"
LABELS = ["orchestra-live-test", TEAM_LABEL]      # the test label the demos' pollers read, and the team's own


class Team:
    def __init__(self, ledger: Ledger, jira: Jira | None = None, project: str | None = None,
                 discord: Discord | None = None, channels: list[str] | None = None, dry_run: bool = False,
                 out: Callable[[str], None] = print, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        self.ledger, self.jira, self.project, self.discord = ledger, jira, project, discord
        self.channels, self.dry_run, self.out = list(channels or []), dry_run, out
        self.clock, self.sleep = clock, sleep
        self.people: list[str] = []
        self.names: dict[str, str] = {}
        self.failures = 0

    # what the day needs to know first (reads)
    def look(self) -> None:
        if self.jira is not None:
            try:
                self.people = self.jira.people(self.project)
            except WireError as e:
                self.out(f"jira: the assignable people could not be read ({e}); assignments are skipped")
        for c in self.channels:
            try:
                self.names[c] = self.discord.channel_name(c) if self.discord else c
            except WireError as e:
                self.names[c] = c
                self.out(f"discord: channel {c} could not be read ({e})")

    def _save(self, d: dict, op: str, ref: str, **meta) -> None:
        if not self.dry_run:
            self.ledger.note(d, op, ref, **meta)

    def _wait_until(self, t: float) -> None:
        if not self.dry_run and (gap := t - self.clock()) > 0:
            self.sleep(gap)

    def play(self, d: dict, plan: Day, rounds: int, slot_s: float) -> int:
        """Play up to ``rounds`` of the day from where ``d`` (its ledger entry) stopped; the rounds played."""
        if self.dry_run:
            d = copy.deepcopy(d)
        played = 0
        while played < rounds and d["played"] < len(plan.rounds):
            r, start = d["played"], self.clock()
            acts = plan.rounds[r]
            self.out(f"round {r + 1}/{len(plan.rounds)} ({9 + r:02d}:00), {len(acts)} acts")
            for i, a in enumerate(acts):
                self._wait_until(start + i * slot_s / max(1, len(acts)))
                self.act(d, a)
            d["played"], played = r + 1, played + 1
            if not self.dry_run:
                self.ledger.save()
            if played < rounds and d["played"] < len(plan.rounds):
                self._wait_until(start + slot_s)
        return played

    def act(self, d: dict, a: Act) -> None:
        side = self.jira if a.system == "jira" else self.discord
        if side is None or (a.system == "discord" and not self.channels):
            self.out(f"  {a.system} {a.op} {a.ref}: skipped ({a.system} is not configured)")
            return
        try:
            line = (self._jira if a.system == "jira" else self._discord)(d, a)
            self.out(f"  {a.system} {a.op} {a.ref}{': ' + line if line else ''}{' (dry run)' if self.dry_run else ''}")
        except WireError as e:
            self.failures += 1
            self.out(f"  {a.system} {a.op} {a.ref}: failed ({e})")
            self._save(d, "failed", a.ref, act=a.op, status=e.status)

    def _key(self, d: dict, ref: str) -> str:
        t = d["tickets"].get(ref)
        if t is None:
            raise WireError(0, "-", ref, "the ticket was never created")
        return t["key"]

    def _jira(self, d: dict, a: Act) -> str:
        if a.op == "create":
            if self.dry_run:
                d["tickets"][a.ref] = {"key": f"<{a.ref}>", "id": None, "created": now(), "closed": None}
                return f"{a.kind} in {self.project}: {a.text}"
            key, iid = self.jira.create(self.project, a.kind, a.text, LABELS)
            d["tickets"][a.ref] = {"key": key, "id": iid, "created": now(), "closed": None}
            self._save(d, "create", a.ref, key=key)
            return f"{key} ({a.kind}): {a.text}"
        key = self._key(d, a.ref)
        if a.op == "assign":
            if not self.people:
                return f"{key}: skipped (no assignable people)"
            who = self.people[a.who % len(self.people)]
            if not self.dry_run:
                self.jira.assign(key, who)
                self._save(d, "assign", a.ref, key=key, to=who)
            return f"{key} assigned to {who}"
        if a.op == "move":
            if self.dry_run:
                return f"{key} to the {a.kind} stage"
            to = self.jira.move(key, a.kind)
            self._save(d, "move", a.ref, key=key, to=to)
            return f"{key} moved to {to}" if to else f"{key}: no transition to the {a.kind} stage now; left"
        if not self.dry_run:
            self.jira.comment(key, a.text)
            self._save(d, "comment", a.ref, key=key)
        return f"{key}: {a.text}"

    def _discord(self, d: dict, a: Act) -> str:
        if a.op == "post":
            channel = self.channels[a.channel % len(self.channels)]
            mid = f"<{a.ref}>" if self.dry_run else self.discord.post(channel, a.text)
            d["messages"][a.ref] = {"channel": channel, "id": mid, "at": now()}
            self._save(d, "post", a.ref, channel=channel, id=mid)
            return f"{a.kind} in #{self.names.get(channel, channel)}: {a.text}"
        m = d["messages"].get(a.ref)
        if m is None:
            raise WireError(0, "-", a.ref, "the message was never posted")
        where = f"#{self.names.get(m['channel'], m['channel'])}"
        if a.op == "check":
            if not self.dry_run:
                self.discord.check(m["channel"], m["id"])
                self._save(d, "check", a.ref, channel=m["channel"], id=m["id"])
            return f"✅ on {a.ref} in {where}"
        text = a.text
        for t in a.refs():
            text = text.replace("{" + t + "}", d["tickets"].get(t, {}).get("key") or f"<{t}>")
        if not self.dry_run:
            rid = self.discord.post(m["channel"], text, reply_to=m["id"])
            self._save(d, "reply", a.ref, channel=m["channel"], id=rid)
        return f"reply to {a.ref} in {where}: {text}"

    def close(self, resolution: str = "Done", site: str | None = None) -> int:
        """Close every ticket the ledger's days created (on ``site``) and nothing has closed: only one
        whose summary starts with the prefix and that carries the team's label. Deletes nothing."""
        n = 0
        for d, ref, t in self.ledger.open_tickets():
            if site and d.get("jira") and d["jira"].rstrip("/") != site.rstrip("/"):
                continue
            key = t["key"]
            try:
                i = self.jira.issue(key)
                if TEAM_LABEL not in i["labels"] or PREFIX.strip() not in i["summary"]:
                    self.out(f"  {key}: refused (not the team's: no {TEAM_LABEL} label or {PREFIX.strip()!r} summary)")
                    continue
                if i["category"] == "done":
                    t["closed"] = {"at": now(), "status": i["status"], "already": True}
                    self.out(f"  {key}: already {i['status']}")
                elif self.dry_run:
                    self.out(f"  {key}: would close from {i['status']} (resolution {resolution}) (dry run)")
                    continue
                else:
                    to, res = self.jira.close(key, resolution)
                    if to is None:
                        self.failures += 1
                        self.out(f"  {key}: no transition leads to a done status")
                        continue
                    t["closed"] = {"at": now(), "status": to, "resolution": res}
                    self.out(f"  {key}: closed as {to}" + (f", resolution {res}" if res else ", no resolution field"))
            except WireError as e:
                if e.status == 404:
                    t["closed"] = {"at": now(), "gone": True}
                    self.out(f"  {key}: not on the board any more (404); noted")
                else:
                    self.failures += 1
                    self.out(f"  {key}: failed ({e})")
                    continue
            n += 1
            if not self.dry_run:
                self.ledger.note(d, "close", ref, key=key)
        return n
