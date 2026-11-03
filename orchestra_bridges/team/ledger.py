"""The scripted team's ledger: a JSON file of what each day created, so ``--close`` and a later audit
find it. Metadata only (keys, ids, channels, times, what was done): never a summary, comment or
message. Written after every write the team makes (a temporary file, then a rename), so a stopped
run leaves an exact record.

    {"days": [{"started": "...", "seed": 1, "jira": "https://…", "project": "SCRUM",
               "channels": ["…"], "rounds": 8, "played": 3,
               "tickets": {"t1": {"key": "SCRUM-51", "id": "10051", "created": "…", "closed": null}},
               "messages": {"m1": {"channel": "…", "id": "…", "at": "…"}},
               "log": [{"at": "…", "op": "move", "ref": "t1", "to": "In Progress"}]}]}"""

from __future__ import annotations

import json
import os
from datetime import UTC, datetime
from pathlib import Path

DEFAULT = Path("~/.local/state/orchestra-bridges/team/ledger.json")


def now() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class Ledger:
    def __init__(self, path: str | Path):
        self.path = Path(path).expanduser()
        self.data = json.loads(self.path.read_text()) if self.path.exists() else {"days": []}
        self.data.setdefault("days", [])

    @property
    def days(self) -> list[dict]:
        return self.data["days"]

    def current(self) -> dict | None:
        """The last day, if it has rounds left to play."""
        d = self.days[-1] if self.days else None
        return d if d is not None and d["played"] < d["rounds"] else None

    def new_day(self, seed: int, rounds: int, jira: str | None, project: str | None, channels: list[str]) -> dict:
        d = {"started": now(), "seed": seed, "jira": jira, "project": project, "channels": list(channels),
             "rounds": rounds, "played": 0, "tickets": {}, "messages": {}, "log": []}
        self.days.append(d)
        return d

    def open_tickets(self) -> list[tuple[dict, str, dict]]:
        """Every ticket a day created and nothing has closed yet: (day, ref, ticket)."""
        return [(d, ref, t) for d in self.days for ref, t in d["tickets"].items() if not t.get("closed")]

    def note(self, d: dict, op: str, ref: str, **meta) -> None:
        d["log"].append({"at": now(), "op": op, "ref": ref, **meta})
        self.save()

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(self.data, indent=2) + "\n")
        os.replace(tmp, self.path)
