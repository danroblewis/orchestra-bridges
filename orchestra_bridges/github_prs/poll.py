"""The reads of one poll, each a GitHub REST call (paged where GitHub pages):

- ``/repos/<repo>/pulls?state=open``: every open pull request, every round. Their state now
  (labels, reviewers still asked, head, draft) is what the measures count, and the list says which
  to follow;
- ``/repos/<repo>/pulls?state=closed&sort=updated&direction=desc``: until one was last updated
  before the window, for the merges and closes since;
- ``/repos/<repo>/issues/<n>/timeline``: per followed pull request, every round while it is open
  (a review does not always move a pull request's ``updated_at``, so that cannot say which to
  read). It holds reviews, conversation comments, label changes, review requests, force pushes,
  commits, merges, closes and reopens;
- ``/repos/<repo>/pulls/<n>/comments``: when a pull request has a new review, for the review's line
  comments (the per-review list leaves out their lines and sides);
- ``/repos/<repo>/commits/<sha>``: once per new head, for the GitHub account behind it;
- ``/repos/<repo>/commits/<sha>/check-runs`` and ``/statuses``: per followed pull request read,
  for its head, unless ``checks = false``.

So a round costs about 2 + 3 per open followed pull request requests (1 + 1 without checks)."""

from __future__ import annotations

from datetime import UTC, datetime

from ..github.api import GitHub
from .translate import parse_time


def iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def open_pulls(api: GitHub, repo: str) -> list[dict]:
    return [p for page in api.pages(f"/repos/{repo}/pulls", {"state": "open", "sort": "created", "direction": "asc"})
            for p in page]


def closed_since(api: GitHub, repo: str, t: float) -> list[dict]:
    out: list[dict] = []
    for page in api.pages(f"/repos/{repo}/pulls", {"state": "closed", "sort": "updated", "direction": "desc"}):
        out += [p for p in page if parse_time(p["updated_at"]) >= t]
        if not page or parse_time(page[-1]["updated_at"]) < t:
            break
    return out


def timeline(api: GitHub, repo: str, number: int) -> list[dict]:
    return [e for page in api.pages(f"/repos/{repo}/issues/{number}/timeline", {}) for e in page]


def line_comments(api: GitHub, repo: str, number: int) -> list[dict]:
    """Every comment on a line of the pull request's diff (each names the review it belongs to)."""
    return [c for page in api.pages(f"/repos/{repo}/pulls/{number}/comments", {}) for c in page]


def commit(api: GitHub, repo: str, sha: str) -> dict:
    return api.get(f"/repos/{repo}/commits/{sha}")


def check_runs(api: GitHub, repo: str, sha: str) -> list[dict]:
    """The head's check runs: the first 100, which is every one but on the busiest repositories."""
    return list(api.get(f"/repos/{repo}/commits/{sha}/check-runs", {"per_page": 100}).get("check_runs") or [])


def statuses(api: GitHub, repo: str, sha: str) -> list[dict]:
    return [s for page in api.pages(f"/repos/{repo}/commits/{sha}/statuses", {}) for s in page]
