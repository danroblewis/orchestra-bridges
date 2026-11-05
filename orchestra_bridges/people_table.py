"""The table that combines accounts into people, as proposals (SPEC §8; the owner, 2026-11-04:
"people use different github user names than their jira or slack names, so we would need a table
of some sort to combine them").

What the relays have seen is on the bus: each account a poller relayed is announced
(``orchestra.machine.started`` from ``person.<system>.<account>``) with what the kit keeps of it,
the account itself (a Jira user name, a GitHub login, a Slack member id) and, where the kit keeps
one, the name the system shows (``display_name``; an announcement from before B10 had it as
``name``). ``seen`` reads those, the newest per account; ``propose`` pairs the accounts the identity
map does not list, across systems:

- **exact**: the same display name, ignoring case, accents and spacing (``CONFIDENCE["display_name"]``).
  Two accounts on one system that share it make the match weak: they are namesakes, and which of
  them is the person on the other system is not known;
- **weak**: the same handle (a user name, a login, an email's local part when an announcement
  carries one; never an opaque id: Jira Cloud's, a Slack member id, a number), the same words in
  another order (``"Example, Alice"``), a close spelling, or a handle made from the other's name
  (``aexample``, ``alice.example``).

Each candidate carries a stable id (the same accounts give the same id), its accounts, a proposed
key and name, its evidence and its confidence. Nothing here writes: joining accounts is a claim
about people, and writing it is the owner's yes (``identity.join``, the agents' ``propose_people``).
Today only the Jira kit announces display names, so Slack and GitHub accounts pair by handle until
their kits announce theirs."""

from __future__ import annotations

import difflib
import functools
import hashlib
import re
from dataclasses import dataclass, field

from .identity import People, account_name, plain
from .kits.poller import opaque_id

STARTED = "orchestra.machine.started"
OPAQUE_SYSTEMS = ("slack",)          # a kit that names accounts by member id: no handle to compare
CONFIDENCE = {"display_name": 0.9, "handle": 0.6, "word_order": 0.7, "spelling": 0.6, "name_handle": 0.5,
              "namesakes": 0.4}
CLOSE = 0.85                         # the least spelling ratio that counts as similar


@dataclass(frozen=True)
class Seen:
    """An account a relay announced."""
    system: str
    account: str                     # as the source names it (what the identity map lists)
    source: str
    display_name: str | None = None
    handle: str | None = None        # a readable account name to compare; None for an opaque id


@dataclass
class Candidate:
    id: str
    strength: str                    # exact or weak
    confidence: float
    accounts: list[Seen]
    key: str
    name: str | None
    evidence: list[str] = field(default_factory=list)

    def entry(self) -> dict:
        """What ``identity.join`` writes for it, once the owner said yes."""
        return {"key": self.key, "name": self.name, "accounts": {a.system: a.account for a in self.accounts}}

    def to_dict(self) -> dict:
        return {"id": self.id, "strength": self.strength, "confidence": self.confidence,
                "accounts": [{"system": a.system, "account": a.account, "source": a.source,
                              **({"display_name": a.display_name} if a.display_name else {})} for a in self.accounts],
                "key": self.key, "name": self.name, "evidence": list(self.evidence)}


def _data(e: dict) -> dict:
    """An event's data: the envelope's, or the event itself when it is read flat (the agents' bus)."""
    return e["data"] if isinstance(e.get("data"), dict) else e


def seen(events: list[dict]) -> list[Seen]:
    """The accounts announced in ``events`` (oldest first), the newest announcement of each."""
    out: dict[str, Seen] = {}
    for e in events:
        source = str(e.get("source") or "")
        parts = source.split(".", 2)
        if e.get("type") != STARTED or len(parts) < 3 or parts[0] != "person":
            continue
        system, account = parts[1], parts[2]
        d = _data(e)
        shown = d.get("display_name")
        name = d.get("name")
        if not shown and isinstance(name, str) and name and not name.startswith(f"{system}.user."):
            shown = name                                 # announced before B10: the display name as the name
        email = d.get("email")
        if isinstance(email, str) and "@" in email:
            handle = email.split("@", 1)[0].lower()
        elif d.get("name_derived") or system in OPAQUE_SYSTEMS or opaque_id(account):
            handle = None
        else:
            handle = account
        out[source] = Seen(system, account, source, shown.strip() if isinstance(shown, str) and shown.strip() else None,
                           handle)
    return list(out.values())


@functools.lru_cache(maxsize=8192)
def _norm(name: str) -> str:
    return plain(name).casefold()


@functools.lru_cache(maxsize=8192)
def _words(name: str) -> tuple[str, ...]:
    return tuple(sorted(w for w in re.split(r"[\s,._-]+", _norm(name)) if w))


def _close_lengths(x: str, y: str) -> bool:
    """Whether two names are near enough in length for their spelling to be close (difflib's
    ``real_quick_ratio``, without making a matcher for every pair)."""
    return 2 * min(len(x), len(y)) / max(len(x) + len(y), 1) >= CLOSE


def _from_name(handle: str, name: str) -> bool:
    """Whether ``handle`` is made from ``name``: first and last joined (``aliceexample``,
    ``alice.example``, ``alice_example``) or the first's initial and the last (``aexample``)."""
    w = [x for x in re.split(r"\s+", _norm(name)) if x]
    if len(w) < 2:
        return False
    h = re.sub(r"[._-]", "", handle.casefold())
    first, last = w[0], w[-1]
    return h in (first + last, first[0] + last, last + first, last + first[0])


def _pair(a: Seen, b: Seen) -> list[tuple[str, float, str]]:
    """What ties two accounts on two systems: (kind, confidence, evidence) each."""
    out = []
    if a.display_name and b.display_name:
        if _norm(a.display_name) == _norm(b.display_name):
            out.append(("display_name", CONFIDENCE["display_name"],
                        f"the same display name, {a.display_name!r}, on {a.system} and {b.system}"))
        elif _words(a.display_name) == _words(b.display_name):
            out.append(("word_order", CONFIDENCE["word_order"],
                        f"the same words in another order: {a.display_name!r} and {b.display_name!r}"))
        elif _close_lengths(_norm(a.display_name), _norm(b.display_name)):
            m = difflib.SequenceMatcher(None, _norm(a.display_name), _norm(b.display_name), autojunk=False)
            if m.quick_ratio() >= CLOSE and (r := m.ratio()) >= CLOSE:
                out.append(("spelling", round(CONFIDENCE["spelling"] * r, 2),
                            f"close spellings ({r:.2f}): {a.display_name!r} and {b.display_name!r}"))
    if a.handle and b.handle and a.handle.casefold() == b.handle.casefold():
        out.append(("handle", CONFIDENCE["handle"], f"the same handle, {a.handle!r}, on {a.system} and {b.system}"))
    for x, y in ((a, b), (b, a)):
        if x.handle and y.display_name and _from_name(x.handle, y.display_name):
            out.append(("name_handle", CONFIDENCE["name_handle"],
                        f"{x.system}'s {x.handle!r} is made from {y.system}'s {y.display_name!r}"))
    return out


def _id(accounts: list[Seen]) -> str:
    return "p-" + hashlib.sha1("|".join(sorted(a.source for a in accounts)).encode()).hexdigest()[:8]


def _named(accounts: list[Seen]) -> tuple[str, str | None]:
    """A proposed key and name: the display name most of them show, else a handle."""
    names = [a.display_name for a in accounts if a.display_name]
    name = max(names, key=lambda n: (names.count(n), "," not in n)) if names else None   # "Last, First" last
    handle = next((a.handle for a in accounts if a.handle), None)
    key = re.sub(r"_+", "_", re.sub(r"[^a-z0-9]", "_", account_name(name))).strip("_") if name else ""
    return key or handle or accounts[0].account, name


def propose(accounts: list[Seen], people: People | None = None) -> list[Candidate]:
    """The candidates among ``accounts`` the map does not list: the exact ones first, then the weak,
    each by confidence."""
    people = people or People()
    free = [a for a in accounts if not people.listed(a.system, a.account)]
    ties = {}
    for i, a in enumerate(free):
        for b in free[i + 1:]:
            if a.system != b.system and (found := _pair(a, b)):
                ties[(a.source, b.source)] = found
    # exact: joined by the same display name, transitively
    group = {a.source: a.source for a in free}

    def root(s):
        while group[s] != s:
            group[s] = group[group[s]]
            s = group[s]
        return s
    for (x, y), found in ties.items():
        if any(k == "display_name" for k, _, _ in found):
            group[root(x)] = root(y)
    by_source = {a.source: a for a in free}
    groups: dict[str, list[Seen]] = {}
    for a in free:
        groups.setdefault(root(a.source), []).append(a)
    out: list[Candidate] = []
    exact_of: dict[str, str] = {}
    for members in groups.values():
        if len(members) < 2:
            continue
        evidence = sorted({ev for (x, y), found in ties.items() for k, _, ev in found
                           if k == "display_name" and x in {m.source for m in members}})
        systems = [m.system for m in members]
        crowded = sorted({s for s in systems if systems.count(s) > 1})
        key, name = _named(members)
        if crowded:
            evidence.append(f"{' and '.join(crowded)} {'has' if len(crowded) == 1 else 'have'} more than one account "
                            "with this name: namesakes, so which is the person is not known")
        c = Candidate(_id(members), "weak" if crowded else "exact",
                      CONFIDENCE["namesakes"] if crowded else CONFIDENCE["display_name"],
                      sorted(members, key=lambda m: (m.system, m.account)), key, name, evidence)
        out.append(c)
        for m in members:
            exact_of[m.source] = c.id
    for (x, y), found in ties.items():
        if exact_of.get(x) and exact_of.get(x) == exact_of.get(y):
            continue                                     # already in one exact candidate
        weak = [f for f in found if f[0] != "display_name"] or found
        pair = sorted([by_source[x], by_source[y]], key=lambda m: (m.system, m.account))
        key, name = _named(pair)
        out.append(Candidate(_id(pair), "weak", max(c for _, c, _ in weak), pair, key, name, [ev for _, _, ev in weak]))
    return sorted(out, key=lambda c: (c.strength != "exact", -c.confidence, c.key))


def line(c: Candidate) -> str:
    accounts = " + ".join(f"{a.system}:{a.account}" + (f" ({a.display_name})" if a.display_name else "") for a in c.accounts)
    return (f"{c.id}  {c.strength} {c.confidence:.2f}  {accounts}  -> [people.{c.key}]"
            + (f' name = "{c.name}"' if c.name else "") + f"  ({'; '.join(c.evidence)})")


SCAN = 20_000                    # the newest events read for announcements (as the agents read the bus)


def from_project(project, scan: int = SCAN) -> list[Candidate]:
    """The candidates from the project's bus and identity map (both of its files)."""
    from orchestra.events import Events

    from .identity import people_files
    events = Events.for_project(project)
    end = events.end()
    evs, _ = events.read(max(0, end - scan), scan)
    people = People.load(people_files(project.config.raw, project.repo_root, project.state_dir))
    return propose(seen(evs), people)


def main(argv: list[str] | None = None) -> int:
    """``orchestra bridges people [--json]``: the candidates, one a line (or as JSON). Read only."""
    import json
    import sys

    from orchestra.config import load_project
    argv = list(argv or [])
    if set(argv) - {"--json"}:
        print("usage: orchestra bridges people [--json]", file=sys.stderr)
        return 2
    found = from_project(load_project())
    if "--json" in argv:
        print(json.dumps([c.to_dict() for c in found], indent=2, ensure_ascii=False))
    else:
        print("\n".join(line(c) for c in found) if found else "no candidates: no two unjoined accounts on two systems match")
    return 0
