"""Who did something: a person or a bot, as the machine an event's source names.

People and bots are machines (docs/proposals/event-sources.md §4 in Orchestra): a bridged event's
source is whoever acted. The owner's identity map, a TOML file that ``[bridges] people`` names,
gives a person a name and a team and joins their accounts on the systems the bridges read::

    [people.alice]
    name = "Alice Example"
    team = "sre"
    github = "alice"
    jira = "alice"
    slack = "U012AB3CD"               # a Slack member id, or the handle

    [people.triage]
    name = "VS Code triage bot"
    team = "vscode"
    bot = true
    github = "vscode-triage-bot"

A listed account's source is ``person.<key>`` (``bot.<key>`` for a bot), so one person is one machine
whichever system they acted in: only the map joins accounts across systems. **An account is not a
person until the map joins it** (SPEC §8): an account the map does not list is announced as an
account, ``<system>.user.<account>`` (``account_name``), with the name the system shows as a detail
(``display_name``), never as its name. The map is two files read together (``people_files``): the
one in the state directory (``<state>/bridges/people.toml``, which the Supervisor writes on the
owner's yes, ``join``) and the owner's own when ``[bridges] people`` names one, whose entries win. An account not listed is
known by its login there: ``person.<login>``, or ``bot.<login>`` for an account the system says is a
bot (GitHub's user type ``Bot``, or a login ending in ``[bot]``, which is dropped). A bridge that is
not the first to name people by bare login asks for its system's **namespace**
(``person.<system>.<login>``; the Slack bridge does), so two strangers who share a login on two
systems stay two machines. The GitHub bridge's sources keep the bare login, as they were first
published.

What the other system does with no account behind it (a Jira change with no author: its own
automation, a workflow post-function, an import) is the system's own act, and its actor is the
system as a bot: ``bot.<system>`` (``bot.jira``), kind program, named for the system. It is a real
actor in that system, never the bridge that relays it. A namespaced bridge's unlisted accounts are
``bot.<system>.<login>`` and so never take that name. Sources are lowercased and keep only letters, digits and ``. _ -``; anything else
becomes ``-``."""

from __future__ import annotations

import json
import re
import tomllib
import unicodedata
from dataclasses import dataclass
from pathlib import Path

_UNSAFE = re.compile(r"[^a-z0-9._-]")
STATE_MAP = Path("bridges") / "people.toml"     # the map the Supervisor writes, under the state directory


class IdentityError(Exception):
    pass


@dataclass(frozen=True)
class Actor:
    source: str                 # the machine: person.<key> or bot.<key>
    login: str | None           # the account on the system the event came from; None: the system itself
    bot: bool = False
    name: str | None = None     # the identity map's: the name the owner confirmed
    team: str | None = None

    def announcement(self, system: str) -> dict:
        """What the bridge announces for this actor (``orchestra.machine.started``), all of which
        Orchestra reads: ``kind``, ``mode``, ``package``, ``name`` and ``team`` (as the machine's
        group). ``name`` is the identity map's only: an account the map does not list is named as an
        account by the poller (``Poller.name_account``, SPEC §8). Nothing else is announced."""
        d = {"kind": "program" if self.bot else "person", "mode": "ad hoc", "package": "bridges"}
        if self.name:
            d["name"] = self.name
        if self.team:
            d["team"] = self.team
        return d


def _safe(s: str) -> str:
    return _UNSAFE.sub("-", s.lower()) or "-"


def plain(s: str) -> str:
    """``s`` without accents, its spacing collapsed: what two display names are compared by."""
    s = unicodedata.normalize("NFKD", str(s))
    return " ".join("".join(c for c in s if not unicodedata.combining(c)).split())


def account_name(display_name: str) -> str:
    """An account's name derived from the name the system shows, where its id is opaque (Jira Cloud):
    lowercased, accents dropped, spaces as ``_``, anything but letters, digits and ``. _ -`` as ``-``
    (``"Alice Example"``: ``alice_example``)."""
    return _safe(plain(display_name).replace(" ", "_"))


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
    def load(cls, path: Path | list[Path] | None) -> People:
        """The map in ``path``, or in each of a list of paths, merged by key: a later file's fields
        over an earlier's (its name, its team, its account on a system), the rest kept (None, or a
        missing file: nobody is listed from it)."""
        entries: dict[str, dict] = {}
        for p in path if isinstance(path, list) else [path]:
            for key, e in entries_of(p).items():
                entries[key] = {**entries.get(key, {}), **e} if isinstance(e, dict) else e
        return cls(entries)

    def accounts(self, system: str) -> list[str]:
        """Every account the map lists on ``system``."""
        return sorted(a for s, a in self._by_account if s == system)

    def listed(self, system: str, login: str | None) -> bool:
        """Whether the map lists ``login`` on ``system``."""
        return bool(login) and (system, str(login).lower()) in self._by_account

    def actor(self, system: str, login: str | None, system_says_bot: bool = False, *,
              namespaced: bool = False) -> Actor:
        """The actor behind ``login`` on ``system``. A deleted account (no login) is ``ghost``, as
        GitHub shows it. With ``namespaced``, an account the map does not list is
        ``person.<system>.<login>``."""
        login = (login or "ghost").lower()
        entry = self._by_account.get((system, login))
        if entry:
            return Actor(f"{'bot' if entry.bot else 'person'}.{entry.key}", login, entry.bot, entry.name, entry.team)
        bot = system_says_bot or login.endswith("[bot]")
        key = login.removesuffix("[bot]") if bot else login
        space = f"{_safe(system)}." if namespaced else ""
        return Actor(f"{'bot' if bot else 'person'}.{space}{_safe(key)}", login, bot)

    def itself(self, system: str, name: str) -> Actor:
        """The other system as the actor of what it did with no account behind it: ``bot.<system>``,
        named ``name`` (``Jira``)."""
        return Actor(f"bot.{_safe(system)}", None, True, name)


def entries_of(path: Path | None) -> dict[str, dict]:
    """The ``[people.<key>]`` tables of the map in ``path`` (none for no file)."""
    if path is None or not path.exists():
        return {}
    try:
        raw = tomllib.loads(path.read_text())
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise IdentityError(f"{path}: {e}") from e
    return dict(raw.get("people") or {})


def people_file(raw: dict, repo_root: Path) -> Path | None:
    """The owner's identity map's file, as ``[bridges] people`` names it (relative to the repository)."""
    p = (raw.get("bridges") or {}).get("people")
    return (repo_root / p) if isinstance(p, str) and p else None


def state_map(state_dir: Path) -> Path:
    """The identity map in the state directory: the one the Supervisor writes (``join``)."""
    return Path(state_dir) / STATE_MAP


def people_files(raw: dict, repo_root: Path, state_dir: Path | None) -> list[Path]:
    """Every file of the identity map, in the order they are read: the state directory's, then the
    owner's own (``[bridges] people``), whose entries win."""
    return [p for p in ((state_map(state_dir) if state_dir else None), people_file(raw, repo_root)) if p]


def _toml(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, list):
        return "[" + ", ".join(_toml(x) for x in v) + "]"
    return json.dumps(str(v), ensure_ascii=False)       # a TOML basic string is JSON's


def join(path: Path, entries: list[dict]) -> list[str]:
    """Write ``entries`` (each ``{"key", "name", "team"?, "accounts": {system: account}}``) into the map
    at ``path``, an entry's accounts added to one already under its key; the keys written. Only on the
    owner's yes (SPEC §8): a join is a claim about people. An account another key lists is refused."""
    have = entries_of(path)
    taken = {(sys_, str(a).lower()): k for k, e in have.items() if isinstance(e, dict)
             for sys_, acc in e.items() if sys_ not in ("name", "team", "bot")
             for a in (acc if isinstance(acc, list) else [acc])}
    written = []
    for e in entries:
        key = _safe(str(e.get("key") or ""))
        if key in ("", "-") or not e.get("accounts"):
            raise IdentityError(f"an entry needs a key and its accounts: {e!r}")
        row = dict(have.get(key) or {})
        for k in ("name", "team"):
            if e.get(k) and not row.get(k):
                row[k] = str(e[k])
        for system, account in sorted(e["accounts"].items()):
            other = taken.get((system, str(account).lower()))
            if other and other != key:
                raise IdentityError(f"{system} account {account!r} is already {other!r}'s in the map")
            listed = row.get(system)
            accounts = listed if isinstance(listed, list) else [listed] if listed else []
            if str(account).lower() not in [str(a).lower() for a in accounts]:
                accounts.append(str(account))
            row[system] = accounts[0] if len(accounts) == 1 else accounts
            taken[(system, str(account).lower())] = key
        have[key] = row
        written.append(key)
    lines = ["# The identity map (orchestra-bridges, SPEC §8): accounts joined into people, each join the owner's yes.", ""]
    for key, row in have.items():
        lines.append(f"[people.{key}]")
        lines += [f"{k} = {_toml(v)}" for k, v in row.items()]
        lines.append("")
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text("\n".join(lines), encoding="utf-8")
    tmp.replace(path)
    return written
