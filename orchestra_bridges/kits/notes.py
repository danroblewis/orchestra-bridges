"""The notes a machine built from the kits says of itself in its announcement, so whoever reads
its card knows what it is not:

- ``fake``: what it reads is a fake on this machine, not the real system (the runner adds it
  whenever ``base_url`` is a loopback host);
- ``stand_in``: it stands in for someone or something that cannot act here (a review posted as a
  comment because GitHub refuses an author's approval);
- ``disabled``: it runs but does nothing, and why (no token, a refused scope).

They go in ``orchestra.machine.started`` beside ``mode`` and ``kind``, one short line each, on the
announcement of the machine they are about: for a poller, each actor it relays (the actors are the
machines; the poller never announces itself). Orchestra keeps them with that machine's card
(``about``) and reads nothing else of them. A note never holds text from the system: it is the
program's own words."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping

KEYS = ("fake", "stand_in", "disabled")
MAX = 200
FAKE = "relayed from a fake on this machine, not the real system"


class NoteError(ValueError):
    pass


def check(notes: Mapping | None, where: str = "notes") -> dict[str, str]:
    """``notes`` as a plain dict, or NoteError: only ``KEYS``, each a short line of its own."""
    out: dict[str, str] = {}
    for k, v in (notes or {}).items():
        if k not in KEYS:
            raise NoteError(f"{where}: {k!r} is no note (the notes are {', '.join(KEYS)})")
        if v is None or v == "":
            continue
        if not isinstance(v, str) or "\n" in v or len(v) > MAX:
            raise NoteError(f"{where}.{k}: one line of at most {MAX} characters")
        out[k] = v
    return out


def stamp(notes: Mapping) -> str:
    """A short hash of the notes, so a changed note is announced again (and none, never)."""
    return hashlib.sha1(json.dumps(dict(sorted(notes.items()))).encode()).hexdigest()[:10]
