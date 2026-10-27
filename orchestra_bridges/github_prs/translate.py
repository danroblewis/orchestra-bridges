"""What GitHub's API says happened on a pull request, as events to publish: metadata only, ids stable.

Every type starts with ``github.pr.``, so these never mix with the issues bridge's in Orchestra's
counts by type. The subject is the pull request (``owner/repo#12``), the time GitHub's, the source
whoever acted (the bridge maps the login, ``identity.py``).

**Never text.** Titles, bodies, review bodies, comment bodies, diff hunks and commit messages pass
through memory and are never copied. ``data`` holds, for every event:

- ``repository``, ``pull`` (the number), ``login`` (who acted), ``branch`` (the head branch),
  ``base`` (the base branch);
- ``head``: the commit the event is about. For a review or a review comment, the commit reviewed;
  for a push, the new head; for a check, the commit checked; for a merge, the head that was
  merged. For the rest, the pull request's head when the bridge read it.

and per type: a label's name, a reviewer's login or a team's slug, a review's state and how many
comments it holds, a review comment's file, line and side, a check's name and conclusion. A label,
a check's name and a file path are names the repository defines, not something a person wrote to
someone.

**Stable ids**, from GitHub's own: ``github:<repo>:pr:<n>:opened``, ``…:event:<issue event id>``
(labels, review requests, merge, close, reopen), ``…:review:<id>``, ``…:review_comment:<id>``,
``…:comment:<id>``, ``…:push:<sha>``, ``…:check:<check run id>``, ``…:status:<status id>``."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

TYPES = ("github.pr.opened", "github.pr.pushed", "github.pr.review_requested", "github.pr.review_request_removed",
         "github.pr.commented", "github.pr.review_submitted", "github.pr.review_commented", "github.pr.labelled",
         "github.pr.unlabelled", "github.pr.check_completed", "github.pr.reopened", "github.pr.merged",
         "github.pr.closed")
RANK = {t: i for i, t in enumerate(TYPES)}         # the order of things GitHub stamps with one second
ISSUE_EVENTS = {"labeled": "github.pr.labelled", "unlabeled": "github.pr.unlabelled",
                "review_requested": "github.pr.review_requested",
                "review_request_removed": "github.pr.review_request_removed",
                "merged": "github.pr.merged", "closed": "github.pr.closed", "reopened": "github.pr.reopened"}
REVIEW_STATES = ("approved", "changes_requested", "commented", "dismissed")
STATUS_DONE = ("success", "failure", "error")


def parse_time(s: str) -> float:
    return datetime.fromisoformat(s).timestamp()


def _time(s: str) -> str:
    return datetime.fromisoformat(s).isoformat()          # 2026-10-27T06:14:09Z -> ...+00:00


def who(user: dict | None) -> tuple[str | None, bool]:
    if not isinstance(user, dict):
        return None, False
    return user.get("login"), user.get("type") == "Bot"


def prefix(repo: str, number: int | None = None) -> str:
    """The start of every id this bridge gives an event of ``repo`` (of one pull request)."""
    return f"github:{repo}:pr:" + ("" if number is None else f"{number}:")


@dataclass(frozen=True)
class Pull:
    """A pull request as the pulls list shows it now."""
    repo: str
    number: int
    branch: str
    base: str
    head: str
    login: str | None
    bot: bool
    association: str | None
    draft: bool
    state: str                               # open or closed
    merged: bool
    created_at: str
    labels: tuple[str, ...]
    reviewers: tuple[str, ...]               # review requested and not yet given: logins
    teams: tuple[str, ...]                   # and team slugs

    @property
    def subject(self) -> str:
        return f"{self.repo}#{self.number}"

    def data(self, login: str | None, head: str | None = None) -> dict:
        return {"repository": self.repo, "pull": self.number, "login": login, "branch": self.branch,
                "base": self.base, "head": head or self.head}


def pull(repo: str, p: dict) -> Pull:
    login, bot = who(p.get("user"))
    return Pull(repo, p["number"], (p.get("head") or {}).get("ref") or "", (p.get("base") or {}).get("ref") or "",
                (p.get("head") or {}).get("sha") or "", login, bot, p.get("author_association"), bool(p.get("draft")),
                p.get("state") or "open", bool(p.get("merged_at")), p["created_at"],
                tuple(lb.get("name") for lb in p.get("labels") or [] if lb.get("name")),
                tuple(u.get("login") for u in p.get("requested_reviewers") or [] if u.get("login")),
                tuple(t.get("slug") for t in p.get("requested_teams") or [] if t.get("slug")))


@dataclass(frozen=True)
class Seen:
    id: str
    type: str
    time: str                   # RFC 3339, GitHub's
    pull: Pull = field(compare=False)
    login: str | None           # None: a deleted account
    bot: bool
    data: dict = field(default_factory=dict, compare=False)

    @property
    def subject(self) -> str:
        return self.pull.subject

    @property
    def ts(self) -> float:
        return parse_time(self.time)


def opening_head(p: Pull, timeline: list[dict]) -> str:
    """The head when the pull request was opened, as near as the timeline says: its last commit
    dated no later than the opening (a commit's date is git's, not the push's), else the head now."""
    opened_at = parse_time(p.created_at)
    before = [e["sha"] for e in timeline if e.get("event") == "committed" and e.get("sha")
              and parse_time(((e.get("committer") or {}).get("date")) or p.created_at) <= opened_at]
    return before[-1] if before else p.head


def opened(p: Pull, head: str | None = None) -> Seen:
    data = {**p.data(p.login, head), "association": p.association, "draft": p.draft}
    return Seen(f"{prefix(p.repo, p.number)}opened", "github.pr.opened", _time(p.created_at), p, p.login, p.bot, data)


def from_issue_event(p: Pull, ev: dict) -> Seen | None:
    """A timeline entry that is an issue event this bridge publishes (a label added or removed, a
    review requested or withdrawn, a merge, a close, a reopen); None for any other."""
    type_ = ISSUE_EVENTS.get(ev.get("event"))
    if type_ is None or ev.get("id") is None or not ev.get("created_at"):
        return None
    login, bot = who(ev.get("actor"))
    data = p.data(login)
    if type_ in ("github.pr.labelled", "github.pr.unlabelled"):
        data["label"] = (ev.get("label") or {}).get("name")
    if type_ in ("github.pr.review_requested", "github.pr.review_request_removed"):
        data["reviewer"] = (ev.get("requested_reviewer") or {}).get("login")
        data["team"] = (ev.get("requested_team") or {}).get("slug")
    if type_ == "github.pr.merged":
        data["merge_commit"] = ev.get("commit_id")
    if type_ == "github.pr.closed":
        data["merged"] = False
    return Seen(f"{prefix(p.repo, p.number)}event:{ev['id']}", type_, _time(ev["created_at"]), p, login, bot, data)


def commented(p: Pull, c: dict) -> Seen | None:
    """A timeline ``commented`` entry: a comment on the conversation (not on a line of the diff)."""
    if c.get("id") is None or not c.get("created_at"):
        return None
    login, bot = who(c.get("user") or c.get("actor"))
    data = {**p.data(login), "comment": c["id"], "association": c.get("author_association")}
    return Seen(f"{prefix(p.repo, p.number)}comment:{c['id']}", "github.pr.commented", _time(c["created_at"]), p,
                login, bot, data)


def reviewed(p: Pull, r: dict, comments: list[dict]) -> list[Seen]:
    """A submitted review (a timeline ``reviewed`` entry) and the comments it holds on lines of the
    diff, each stamped with the review's time, when they became visible. A pending review is not
    submitted and is left out."""
    state = (r.get("state") or "").lower()
    if state not in REVIEW_STATES or r.get("id") is None or not r.get("submitted_at"):
        return []
    login, bot = who(r.get("user"))
    at = _time(r["submitted_at"])
    commit = r.get("commit_id")
    out = [Seen(f"{prefix(p.repo, p.number)}review:{r['id']}", "github.pr.review_submitted", at, p, login, bot,
                {**p.data(login, commit), "review": r["id"], "state": state, "comments": len(comments),
                 "association": r.get("author_association")})]
    for c in comments:
        out.append(Seen(f"{prefix(p.repo, p.number)}review_comment:{c['id']}", "github.pr.review_commented", at, p,
                        login, bot,
                        {**p.data(login, c.get("commit_id") or commit), "review": r["id"], "comment": c["id"],
                         "path": c.get("path"), "line": c.get("line") if c.get("line") is not None else c.get("original_line"),
                         "start_line": c.get("start_line"), "side": c.get("side"), "subject_type": c.get("subject_type"),
                         "in_reply_to": c.get("in_reply_to_id")}))
    return out


def pushed(p: Pull, at: str, login: str | None, bot: bool, previous: str | None, *, forced: bool, by: str) -> Seen:
    """A new head on the pull request's branch. ``by`` says how the pusher was known: ``force_push``
    (GitHub's record of a force push), ``committer`` or ``author`` (the head commit's GitHub
    account), or ``pull_author`` (none: the pull request's author stands in)."""
    return Seen(f"{prefix(p.repo, p.number)}push:{p.head}", "github.pr.pushed", _time(at), p, login, bot,
                {**p.data(login), "previous": previous, "forced": forced, "pusher_from": by})


def check_run(p: Pull, run: dict) -> Seen | None:
    """A check run on the head that has completed; its source is the app that ran it."""
    if run.get("status") != "completed" or not run.get("completed_at") or run.get("id") is None:
        return None
    slug = (run.get("app") or {}).get("slug") or "unknown-app"
    login = f"{slug}[bot]"
    return Seen(f"{prefix(p.repo, p.number)}check:{run['id']}", "github.pr.check_completed", _time(run["completed_at"]),
                p, login, True,
                {**p.data(login, run.get("head_sha")), "check": run.get("name"), "conclusion": run.get("conclusion"),
                 "via": "check_run", "app": slug})


def status(p: Pull, st: dict, sha: str) -> Seen | None:
    """A commit status on the head that is final (success, failure or error); pending is not."""
    if st.get("state") not in STATUS_DONE or st.get("id") is None or not st.get("created_at"):
        return None
    login, bot = who(st.get("creator"))
    return Seen(f"{prefix(p.repo, p.number)}status:{st['id']}", "github.pr.check_completed", _time(st["created_at"]),
                p, login, bot,
                {**p.data(login, sha), "check": st.get("context"), "conclusion": st["state"], "via": "status"})
