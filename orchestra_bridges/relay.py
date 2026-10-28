"""A bridge is a relay, never a machine (Orchestra's AGENTS.md rule 2, decision D19).

It puts on the bus only what the other system's actors did, as those actors: every source is a
person or a bot (``identity.py``), ``person.<…>`` or ``bot.<…>``. A publish under any other source
(the bridge's own name, ``bridges.<system>``; Orchestra's host, the default source of
``orchestra.events.Events``; or none) is refused here, so no bridge can announce itself or publish
as itself by mistake. Reading the bus back (``after``) is passed through as it is."""

from __future__ import annotations

from orchestra.events import Events

ACTORS = ("person.", "bot.")


class RelayError(ValueError):
    pass


class Relay:
    def __init__(self, events: Events):
        self.events = events

    def publish(self, type: str, data=None, *, source: str, **ext) -> dict:
        if not isinstance(source, str) or not source.startswith(ACTORS):
            raise RelayError(f"{type}: a bridge publishes only as the person or bot who acted, never as {source!r}")
        return self.events.publish(type, data, source=source, **ext)

    def after(self, offset: int) -> tuple[list[dict], int]:
        return self.events.after(offset)
