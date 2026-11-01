"""The skeleton of a poller: a program that reads another system on an interval and relays what
the people and bots there did as events on the project's bus.

::

    poller = Poller("pagerduty_alerts", every_s=300)
    def poll(p):
        for inc in api.get("/incidents", {...}, keep=meta)["incidents"]:
            p.emit(f"pagerduty.incident.{inc['status']}", inc["id"], {"urgency": inc["urgency"]},
                   source=actor_source("pagerduty", inc["by"]), stamp=inc["changed_at"])
        p.cursor = newest
    poller.run(poll)

- **A relay, never a machine** (Orchestra's rule 2, D19: the provider is never the machine; the
  bridges' SPEC §2). Every event's ``source`` is who acted, ``person.<system>.<account>`` or
  ``bot.<system>`` (``actor_source``); any other source is refused (``RelayError``). The poller
  never announces itself and never publishes under its own name. Each actor is **announced** once,
  just before their first event: ``orchestra.machine.started`` from their source, with ``mode``
  (``ad hoc``), ``kind`` (``person``, or ``program`` for a bot), ``listens_for`` (none) and
  ``package`` (the poller's name), and the poller's notes (``notes.py``: ``fake``, ``stand_in``,
  ``disabled``), so the actor's own card says it was relayed from a fake. An announcement is
  emitted once, across restarts too; a changed note announces each actor again, at their next event.
- **The interval loop**: ``run(poll)`` calls ``poll(poller)`` now and every ``every_s`` until
  SIGTERM or SIGINT (or ``stop()``), which ends it between polls or in the wait. A poll that raises
  is logged by its exception's type and message and tried again next interval; its cursor and
  counters are not saved.
- **The cursor** (``poller.cursor``): whatever the poll sets, saved in ``cursor.json`` after each
  good poll and read back at start (``pollstate.py``).
- **Idempotent emission**: the id is ``<name>:<type>:<key>@<stamp>``, the key being the item's id
  in the other system (the subject, by default) and the stamp its change (an updated time, a
  version; by default a hash of the data). An id already emitted is skipped, across restarts too.
- **Series**: ``gauge`` and ``counter`` are written after each good poll, as Prometheus text, to
  ``metrics_path`` (``-``: standard output) for a ``[[collect]]`` entry (``exposition.py``). The
  instrument stores them, never the poller. A measurement that one cheap read can make is better
  as the scaffold's ``measure`` (the ``[[collect]]`` entry runs the program with ``measure``)."""

from __future__ import annotations

import hashlib
import json
import re
import signal
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any

from orchestra.events import Events

from . import notes as note
from .exposition import Registry
from .pollstate import PollState

NAME = re.compile(r"^[a-z][a-z0-9_]{1,40}$")
STARTED = "orchestra.machine.started"
ACTORS = ("person.", "bot.")            # the only sources a poller publishes as: who acted
__all__ = ["ACTORS", "Poller", "RelayError", "actor_source", "poll_dir"]


class RelayError(ValueError):
    """A publish under a source that is not a person or a bot (the poller's own name, say)."""


def _safe(s: str) -> str:
    return re.sub(r"[^a-z0-9._-]", "-", str(s).lower())


def actor_source(system: str, account: str | None = None, *, bot: bool = False) -> str:
    """Who acted, as a source: ``person.<system>.<account>``, ``bot.<system>.<account>`` for a bot
    account, or ``bot.<system>`` for the system's own act (no account behind it)."""
    if not account:
        return f"bot.{_safe(system)}"
    return f"{'bot' if bot else 'person'}.{_safe(system)}.{_safe(account)}"


def poll_dir(name: str, state_dir: str | Path | None = None, project=None) -> Path | None:
    """Where the poller ``name`` keeps its cursor, emitted ids and counters: ``state_dir`` itself
    when given (a test's), else ``<state>/kits/pollers/<name>/`` of ``project``; None without either."""
    if state_dir:
        return Path(state_dir)
    return project.state_dir / "kits" / "pollers" / name if project is not None else None


def stamp_of(type: str, data: Any) -> str:
    return hashlib.sha1(json.dumps([type, data], sort_keys=True, default=str).encode()).hexdigest()[:16]


class Poller:
    def __init__(self, name: str, every_s: float = 300.0, state_dir: str | Path | None = None, *,
                 events: Events | None = None, metrics_path: str | Path | None = None, project=None,
                 package: str | None = None, log=None, notes: dict[str, str] | None = None):
        if not NAME.match(name):
            raise ValueError(f"{name!r}: a poller's name is lower case letters, digits and _ (2 to 41)")
        if every_s <= 0:
            raise ValueError("every_s must be positive")
        self.name, self.every_s, self.package = name, float(every_s), package or name
        self.notes = note.check(notes)
        self._project = project
        if state_dir:
            root, default_metrics = Path(state_dir), Path(state_dir) / "metrics.prom"
        else:
            project = self._load_project()
            root, default_metrics = poll_dir(name, None, project), project.state_dir / "kits" / "metrics" / f"{name}.prom"
        self.state = PollState(root)
        self.metrics_path = metrics_path if metrics_path is not None else default_metrics
        self.events = events
        self.series = Registry()
        self.series.load_counters(self.state.counters)
        self._stop = threading.Event()
        self._log = log or (lambda msg: print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {name}: {msg}",
                                              file=sys.stderr, flush=True))
        self.polls = self.failures = 0
        self.published: list[dict] = []          # this poll's events (announcements included)

    def _load_project(self):
        if self._project is None:
            from orchestra.config import load_project
            self._project = load_project()
        return self._project

    def _events(self) -> Events:
        if self.events is None:
            self.events = Events.for_project(self._load_project())
        return self.events

    @property
    def cursor(self) -> Any:
        return self.state.cursor

    @cursor.setter
    def cursor(self, value: Any) -> None:
        self.state.cursor = value

    def event_id(self, type: str, key: str, stamp: str) -> str:
        return f"{self.name}:{type}:{key}@{stamp}"

    def _publish(self, eid: str, type: str, data: Any, source: str, **ext) -> dict:
        ev = self._events().publish(type, data, source=source, id=eid, **ext)
        self.state.mark(eid)
        self.published.append(ev)
        return ev

    def announce(self, source: str, time: str | None = None, **about: Any) -> dict | None:
        """``orchestra.machine.started`` for an actor, once (once more after the notes change): None
        when it was announced before. ``about`` (a name, a team) and the notes go to its card unread."""
        notes = note.check(self.notes)
        eid = f"{self.name}:announce:{source}" + (f"#{note.stamp(notes)}" if notes else "")
        if self.state.seen(eid):
            return None
        data = {"mode": "ad hoc", "kind": "program" if source.startswith("bot.") else "person",
                "listens_for": [], "package": self.package, **notes, **about}
        return self._publish(eid, STARTED, data, source, **({"time": time} if time else {}))

    def emit(self, type: str, subject: str | None, data: Any = None, source: str | None = None, *,
             key: str | None = None, stamp: str | None = None, time: str | None = None) -> dict | None:
        """Publish ``type`` about ``subject`` as ``source`` (who acted), once: None when its id was
        emitted before. The actor is announced first, the first time."""
        if not isinstance(source, str) or not source.startswith(ACTORS):
            raise RelayError(f"{type}: a poller publishes only as the person or bot who acted "
                             f"(person.<system>.<account>, bot.<system>), never as {source!r}")
        if type == STARTED:
            raise RelayError("announcements are the poller's own (announce), made once per actor")
        k = key if key is not None else subject
        if k is None:
            raise ValueError(f"{type}: an event needs the item's id (subject or key) to be emitted once")
        eid = self.event_id(type, str(k), str(stamp) if stamp is not None else stamp_of(type, data))
        if self.state.seen(eid):
            return None
        self.announce(source, time)
        ext = {"subject": str(subject)} if subject is not None else {}
        if time is not None:
            ext["time"] = time
        return self._publish(eid, type, data, source, **ext)

    def gauge(self, name: str, value: float, labels: dict[str, str] | None = None, help: str | None = None) -> None:
        self.series.gauge(name, value, labels, help)

    def counter(self, name: str, by: float = 1.0, labels: dict[str, str] | None = None, help: str | None = None) -> None:
        self.series.counter(name, by, labels, help)

    def poll_once(self, poll: Callable[[Poller], Any]) -> bool:
        """One poll: True when it succeeded (its cursor, counters and series saved)."""
        self.published = []
        self.series.clear_gauges()
        before_cursor, before_counters = self.state.cursor, self.series.counters_state()
        try:
            poll(self)
        except Exception as e:  # noqa: BLE001 - one bad poll never ends the poller
            self.failures += 1
            self.state.cursor = before_cursor
            self.series.counters.clear()
            self.series.load_counters(before_counters)
            self._log(f"poll failed: {type(e).__name__}: {e}")
            return False
        self.polls += 1
        self.state.counters = self.series.counters_state()
        self.state.save()
        if self.series.gauges or self.series.counters:
            self.series.write(self.metrics_path)
        self._log(f"poll {self.polls}: {len(self.published)} events")
        return True

    def stop(self) -> None:
        self._stop.set()

    def run(self, poll: Callable[[Poller], Any], *, once: bool = False, rounds: int | None = None) -> int:
        """Poll until stopped (SIGTERM, SIGINT, ``stop()``), ``once``, or after ``rounds`` polls.
        Returns 0, or 1 when ``once`` and the poll failed."""
        if threading.current_thread() is threading.main_thread():
            for sig in (signal.SIGTERM, signal.SIGINT):
                signal.signal(sig, lambda *_: self._stop.set())
        n, ok = 0, True
        while not self._stop.is_set():
            ok = self.poll_once(poll)
            n += 1
            if once or (rounds is not None and n >= rounds):
                break
            self._stop.wait(self.every_s)
        self._log("stopped")
        return 0 if ok or not once else 1
