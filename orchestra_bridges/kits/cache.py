"""A TTL cache of answers on disk, one small JSON file per request, under the state directory.

The key is the method, the full URL (with its query) and the body, hashed: never a header, so the
token is in no key and in no file. A file holds what the caller chose to keep of the answer (the
client's ``keep``), its status and when it was stored; it is written whole (to a temporary file,
then renamed) and readable by its owner only. An entry older than its ``ttl_s`` is a miss, but is
still there for a dry run, which serves whatever is cached, however old."""

from __future__ import annotations

import hashlib
import json
import os
import time
from pathlib import Path
from typing import Any


def key(method: str, url: str, body: bytes | None) -> str:
    h = hashlib.sha256()
    for part in (method.upper().encode(), b"\0", url.encode(), b"\0", body or b""):
        h.update(part)
    return h.hexdigest()


class DiskCache:
    def __init__(self, root: Path, clock=time.time):
        self.root = Path(root)
        self.clock = clock

    def _path(self, k: str) -> Path:
        return self.root / k[:2] / f"{k}.json"

    def get(self, k: str, ttl_s: float | None) -> dict | None:
        """The entry ``{"at", "status", "value"}``, or None when absent, unreadable or older than
        ``ttl_s`` (None: any age)."""
        try:
            entry = json.loads(self._path(k).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(entry, dict) or "value" not in entry:
            return None
        if ttl_s is not None and self.clock() - float(entry.get("at", 0)) > ttl_s:
            return None
        return entry

    def put(self, k: str, status: int, value: Any) -> None:
        p = self._path(k)
        p.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        tmp = p.with_suffix(f".{os.getpid()}.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"at": self.clock(), "status": status, "value": value}, fh)
        os.replace(tmp, p)
