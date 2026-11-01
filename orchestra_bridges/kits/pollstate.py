"""What a poller keeps between runs, in its own directory under the state directory:

- ``cursor.json``: the cursor (whatever the poll function sets: a time, an offset, a page token)
  and the counters' totals, written whole after each good poll;
- ``emitted.log``: one event id a line, appended as each event is published, so an event published
  just before a crash is still known after it. It is compacted to the last ``keep`` ids at start.

Nothing in either is text from the other system: ids, stamps and numbers."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

KEEP = 20_000


class PollState:
    def __init__(self, root: Path, keep: int = KEEP):
        self.root = Path(root)
        self.keep = keep
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.cursor: Any = None
        self.counters: dict = {}
        self.emitted: dict[str, None] = {}          # insertion-ordered set
        self._load()

    @property
    def cursor_file(self) -> Path:
        return self.root / "cursor.json"

    @property
    def emitted_file(self) -> Path:
        return self.root / "emitted.log"

    def _load(self) -> None:
        try:
            d = json.loads(self.cursor_file.read_text(encoding="utf-8"))
            self.cursor, self.counters = d.get("cursor"), d.get("counters") or {}
        except (OSError, ValueError, AttributeError):
            pass
        try:
            ids = [x for x in self.emitted_file.read_text(encoding="utf-8").splitlines() if x]
        except OSError:
            ids = []
        self.emitted = dict.fromkeys(ids[-self.keep:])
        if len(ids) > self.keep:
            self._rewrite()

    def _rewrite(self) -> None:
        tmp = self.emitted_file.with_suffix(".tmp")
        tmp.write_text("".join(f"{i}\n" for i in self.emitted), encoding="utf-8")
        os.replace(tmp, self.emitted_file)

    def seen(self, event_id: str) -> bool:
        return event_id in self.emitted

    def mark(self, event_id: str) -> None:
        self.emitted[event_id] = None
        with self.emitted_file.open("a", encoding="utf-8") as fh:
            fh.write(event_id + "\n")

    def save(self) -> None:
        tmp = self.cursor_file.with_suffix(".tmp")
        tmp.write_text(json.dumps({"cursor": self.cursor, "counters": self.counters}), encoding="utf-8")
        os.replace(tmp, self.cursor_file)


def counters_at(root: str | Path | None) -> dict:
    """The counters' totals a poller saved under ``root`` after its last good poll, read without
    creating anything (``{}`` when there are none): what a measurement prints of them."""
    if root is None:
        return {}
    try:
        d = json.loads((Path(root) / "cursor.json").read_text(encoding="utf-8"))
        return d.get("counters") or {}
    except (OSError, ValueError, AttributeError):
        return {}
