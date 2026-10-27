"""Announcing the people and bots a bridge sees, as machines (SPEC §2), the same way for every bridge.

An actor is announced (``orchestra.machine.started``, source the actor) just before their first
event, stamped with that event's time, and again whenever what the bridge would announce differs
from their last announcement on the bus: before their next event, and on start (``reannounce``) for
everyone the identity map lists who was announced before, so a name or team changed in the map
shows even if they do nothing. The id is ``<system>:announce:<source>:<hash of what is said>:<time>``,
so re-reading never announces twice."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Iterable

from orchestra.events import Events

from .identity import Actor, People

STARTED = "orchestra.machine.started"


def prefix(system: str) -> str:
    """The start of the id of every announcement a bridge of ``system`` makes."""
    return f"{system}:announce:"


class Announcer:
    def __init__(self, events: Events, system: str, people: People):
        self.events, self.system, self.people = events, system, people
        self.announced: dict[str, dict] = {}          # source -> its last announcement on the bus

    def from_events(self, own: Iterable[dict]) -> None:
        """Take each actor's last announcement from the bridge's own events (in bus order)."""
        p = prefix(self.system)
        self.announced = {e.get("source"): e.get("data") or {} for e in own
                          if str(e.get("id", "")).startswith(p) and e.get("type") == STARTED}

    def actor(self, actor: Actor, when: str) -> None:
        """Announce ``actor`` unless their last announcement on the bus says the same."""
        about = actor.announcement(self.system)
        if self.announced.get(actor.source) == about:
            return
        digest = hashlib.sha1(json.dumps(about, sort_keys=True).encode()).hexdigest()[:12]
        aid = f"{prefix(self.system)}{actor.source}:{digest}:{when}"
        self.events.publish(STARTED, about, source=actor.source, time=when, id=aid)
        self.announced[actor.source] = about

    def reannounce(self, now: str, actor_of: Callable[[str], Actor] | None = None) -> None:
        """Everyone the identity map lists on this system who was announced before, again when their
        name or team is not what their last announcement says."""
        actor_of = actor_of or (lambda login: self.people.actor(self.system, login))
        for login in self.people.accounts(self.system):
            actor = actor_of(login)
            last = self.announced.get(actor.source)
            if last is not None and (last.get("name"), last.get("team")) != (actor.name, actor.team):
                self.actor(actor, now)
