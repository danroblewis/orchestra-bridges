"""Who did something: a person or a bot, as the machine an event's source names.

People and bots are machines (docs/proposals/event-sources.md §4 in Orchestra): a bridged event's
source is whoever acted. The owner's identity map, a TOML file that ``[bridges] people`` names,
gives a person a name and a team and joins their accounts on the systems the bridges read::

    [people.daniel]
    name = "Daniel Lewis"
    team = "sre"
    github = "danroblewis"

    [people.triage]
    name = "VS Code triage bot"
    team = "vscode"
    bot = true
    github = "vscode-triage-bot"

A listed account's source is ``person.<key>`` (``bot.<key>`` for a bot), so one person is one machine
whichever system they acted in. An account not listed is known by its login there: ``person.<login>``,
or ``bot.<login>`` for an account the system says is a bot (GitHub's user type ``Bot``, or a login
ending in ``[bot]``, which is dropped). Sources are lowercased and keep only letters, digits and
``. _ -``; anything else becomes ``-``."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

_UNSAFE = re.compile(r"[^a-z0-9._-]")


class IdentityError(Exception):
    pass


@dataclass(frozen=True)
class Actor:
    source: str                 # the machine: person.<key> or bot.<key>
    login: str                  # the account on the system the event came from
    bot: bool = False
    name: str | None = None     # from the identity map only
    team: str | None = None

    def announcement(self, system: str) -> dict:
        """What the bridge announces for this actor (``orchestra.machine.started``): Orchestra reads
        ``kind``, ``mode`` and ``package``; the rest goes to the package's card unread."""
        d = {"kind": "program" if self.bot else "person", "mode": "ad hoc", "package": "bridges",
             system: self.login, "bot": self.bot}
        if self.name:
            d["name"] = self.name
        if self.team:
            d["team"] = self.team
        return d


def _safe(s: str) -> str:
    return _UNSAFE.sub("-", s.lower()) or "-"


@dataclass(frozen=True)
class _Entry:
    key: str
    name: str | None
    team: str | None
    bot: bool


class People:
    """The identity map, for one or more systems (each entry names its account per system)."""

    def __init__(self, entries: dict[str, dict] | None = None):
        self._by_account: dict[tuple[str, str], _Entry] = {}
        for key, e in (entries or {}).items():
            if not isinstance(e, dict):
                raise IdentityError(f"[people.{key}] must be a table")
            entry = _Entry(_safe(key), e.get("name"), e.get("team"), bool(e.get("bot", False)))
            for system, account in e.items():
                if system in ("name", "team", "bot"):
                    continue
                for a in account if isinstance(account, list) else [account]:
                    self._by_account[(system, str(a).lower())] = entry

    @classmethod
    def load(cls, path: Path | None) -> People:
        """The map in ``path`` (None, or a missing file: nobody is listed)."""
        if path is None or not path.exists():
            return cls()
        try:
            raw = tomllib.loads(path.read_text())
        except (OSError, tomllib.TOMLDecodeError) as e:
            raise IdentityError(f"{path}: {e}") from e
        return cls(raw.get("people") or {})

    def accounts(self, system: str) -> list[str]:
        """Every account the map lists on ``system``."""
        return sorted(a for s, a in self._by_account if s == system)

    def actor(self, system: str, login: str | None, system_says_bot: bool = False) -> Actor:
        """The actor behind ``login`` on ``system``. A deleted account (no login) is ``ghost``, as
        GitHub shows it."""
        login = (login or "ghost").lower()
        entry = self._by_account.get((system, login))
        if entry:
            return Actor(f"{'bot' if entry.bot else 'person'}.{entry.key}", login, entry.bot, entry.name, entry.team)
        bot = system_says_bot or login.endswith("[bot]")
        key = login.removesuffix("[bot]") if bot else login
        return Actor(f"{'bot' if bot else 'person'}.{_safe(key)}", login, bot)
