"""One read of a repository: everything that happened to its issues since a moment, in time order.

Three lists, each paged:

- ``/repos/<repo>/issues?state=all&sort=updated&direction=asc&since=<t>``: issues touched since
  ``t``; those created since ``t`` were opened then;
- ``/repos/<repo>/issues/comments?sort=updated&direction=asc&since=<t>``: comments touched since
  ``t``; those created since ``t`` are new (an old comment edited is not);
- ``/repos/<repo>/issues/events``: newest first, read until one is older than ``t``.

Pull requests are in all three and are left out (``translate.py``). How far back a busy
repository's issue events can be paged is GitHub's to say; ``max_catchup`` bounds what is asked."""

from __future__ import annotations

from datetime import UTC, datetime

from .api import GitHub
from .translate import RANK, Seen, commented, from_event, opened, parse_time


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def since(api: GitHub, repo: str, t: float) -> list[Seen]:
    out: list[Seen] = []
    after = {"since": iso(t)}
    for page in api.pages(f"/repos/{repo}/issues", {"state": "all", "sort": "updated", "direction": "asc", **after}):
        out += [s for s in (opened(repo, i) for i in page) if s and s.ts >= t]
    for page in api.pages(f"/repos/{repo}/issues/comments", {"sort": "updated", "direction": "asc", **after}):
        out += [s for s in (commented(repo, c) for c in page) if s and s.ts >= t]
    for page in api.pages(f"/repos/{repo}/issues/events", {}):
        out += [s for s in (from_event(repo, e) for e in page) if s and s.ts >= t]
        if not page or parse_time(page[-1]["created_at"]) < t:
            break
    unique = {s.id: s for s in out}
    return sorted(unique.values(), key=lambda s: (s.ts, RANK[s.type], s.id))
