"""What GitHub's API says happened to issues, as events to publish: metadata only, ids stable.

Three lists are read (``poll.py``): the repository's issues (``github.issue.opened``: who opened it
and when), its issue comments (``github.issue.commented``) and its issue events
(``github.issue.labelled``, ``github.issue.closed``, ``github.issue.reopened``). Pull requests are
issues to GitHub; they are left out. Every type starts with the system, so another bridge's issues
(``jira.issue.created``) are never counted with these. Before 2026-10-27 the types had no prefix
(``issue.opened``); ``current`` reads those, so a restart keeps what was published under them.

**Never text.** An issue's title and body and a comment's body pass through memory and are never
copied: ``data`` holds the repository, the issue number, the actor's login and association, a
comment's id, a label's name, a close's reason. A label is a name the repository defines, not
something a person wrote to someone.

**Stable ids**, so a poll that overlaps the last one, or a bridge restarted after a gap, never
publishes a thing twice: ``github:<repo>:opened:<number>``, ``github:<repo>:comment:<id>``,
``github:<repo>:event:<id>``, from GitHub's own ids. The time is GitHub's."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

EVENT_TYPES = {"labeled": "github.issue.labelled", "closed": "github.issue.closed", "reopened": "github.issue.reopened"}
TYPES = ("github.issue.opened", "github.issue.commented", *EVENT_TYPES.values())
RANK = {t: i for i, t in enumerate(("github.issue.opened", "github.issue.commented", "github.issue.labelled", "github.issue.reopened",
                                    "github.issue.closed"))}   # the order of things GitHub stamps with one second
OLD = {t.removeprefix("github."): t for t in TYPES}          # the types as published before they had the prefix


def current(type_: str | None) -> str | None:
    """An event type this bridge published, by its current name (``issue.opened`` is
    ``github.issue.opened``); None when it is not one of its issue types."""
    return type_ if type_ in TYPES else OLD.get(type_ or "")


@dataclass(frozen=True)
class Seen:
    id: str
    type: str
    time: str                   # RFC 3339, GitHub's
    repo: str
    number: int
    login: str | None           # None: a deleted account
    bot: bool                   # GitHub says the account is a bot
    data: dict = field(default_factory=dict, compare=False)

    @property
    def subject(self) -> str:
        return f"{self.repo}#{self.number}"

    @property
    def ts(self) -> float:
        return parse_time(self.time)


def prefix(repo: str) -> str:
    """The start of every id this bridge gives an event of ``repo``."""
    return f"github:{repo}:"


def parse_time(s: str) -> float:
    return datetime.fromisoformat(s).timestamp()


def _time(s: str) -> str:
    return datetime.fromisoformat(s).isoformat()          # 2026-10-27T06:14:09Z -> ...+00:00


def _who(user: dict | None) -> tuple[str | None, bool]:
    if not isinstance(user, dict):
        return None, False
    return user.get("login"), user.get("type") == "Bot"


def opened(repo: str, issue: dict) -> Seen | None:
    """An issue from the issues list, as its opening; None for a pull request."""
    if "pull_request" in issue:
        return None
    login, bot = _who(issue.get("user"))
    return Seen(f"{prefix(repo)}opened:{issue['number']}", "github.issue.opened", _time(issue["created_at"]), repo,
                issue["number"], login, bot,
                {"repository": repo, "issue": issue["number"], "login": login,
                 "association": issue.get("author_association")})


def _number(issue_url: str) -> int | None:
    tail = issue_url.rstrip("/").rsplit("/", 1)[-1]
    return int(tail) if tail.isdigit() else None


def commented(repo: str, comment: dict) -> Seen | None:
    """A comment from the repository's issue comments; None for one on a pull request."""
    if "/pull/" in (comment.get("html_url") or ""):
        return None
    number = _number(comment.get("issue_url") or "")
    if number is None:
        return None
    login, bot = _who(comment.get("user"))
    return Seen(f"{prefix(repo)}comment:{comment['id']}", "github.issue.commented", _time(comment["created_at"]), repo,
                number, login, bot,
                {"repository": repo, "issue": number, "login": login, "comment": comment["id"],
                 "association": comment.get("author_association")})


def from_event(repo: str, ev: dict) -> Seen | None:
    """An entry of the repository's issue events, when it is one this bridge publishes (a label
    added, a close, a reopen) and on an issue rather than a pull request."""
    type_ = EVENT_TYPES.get(ev.get("event"))
    issue = ev.get("issue")
    if type_ is None or not isinstance(issue, dict) or "pull_request" in issue:
        return None
    login, bot = _who(ev.get("actor"))
    data = {"repository": repo, "issue": issue["number"], "login": login}
    if type_ == "github.issue.labelled":
        data["label"] = (ev.get("label") or {}).get("name")
    if type_ == "github.issue.closed":
        data["reason"] = ev.get("state_reason") or issue.get("state_reason")
    return Seen(f"{prefix(repo)}event:{ev['id']}", type_, _time(ev["created_at"]), repo, issue["number"], login, bot, data)
