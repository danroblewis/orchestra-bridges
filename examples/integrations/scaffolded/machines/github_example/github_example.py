"""github_example: a poller that relays what people, bots and checks did to one GitHub repository's issues
and pull requests (scaffolded from the orchestra-bridges kit ``github``; edit freely, AGENTS.md says
what it may do). GitHub's REST API with a token (``Bearer``): ``gh auth token`` is one.

``python github_example.py`` (the poller): each poll reads the pull requests and issues updated since the
last poll (less ``overlap``), newest first, and for each pull request followed (``[settings]
branches`` globs or ``labels``; with neither, all) its issue events and reviews; and for each open
one followed, its head's checks until they are final. It publishes once each, with the item as the
subject (``<owner>/<name>#<number>``) and who acted as the source (``person.github.<login>``;
``bot.github.<app>`` for a bot or an app; ``bot.github`` when no one):

| type | when | source | stamp |
|---|---|---|---|
| ``github.pr.opened`` | opened since the poller began | the author | ``opened`` |
| ``github.pr.synchronize`` | its head moved since the last poll | the author (GitHub does not say who pushed) | the new head |
| ``github.pr.labeled``, ``.unlabeled`` | a label added or taken off | who did it | the event's id |
| ``github.pr.review_requested`` | a review asked of someone (``reviewer``) | who asked | the event's id |
| ``github.pr.approved``, ``.changes_requested``, ``.commented`` | a review submitted, by its verdict | the reviewer | the review's id |
| ``github.checks.completed`` | every check run on the head completed (``outcome`` success unless one failed); with none, a final commit status (``via: status``) | the app, or who set the status | the head |
| ``github.pr.merged``, ``.closed``, ``.reopened`` | merged; closed unmerged; reopened | who did it | the event's id |
| ``github.issue.opened`` | an issue opened since the poller began (``[settings] issues``) | the author | ``opened`` |
| ``github.issue.labeled``, ``.unlabeled``, ``.closed``, ``.reopened`` | as for a pull request | who did it | the event's id |

**The first run** starts at ``[settings] since`` (``now`` by default; or a duration back, ``"1d"``,
or a time: ``start_of``), never at the beginning of the repository's history: nothing done before it
is relayed (a label, a review, a head's checks completed before it), and of a catch-up window it
relays only the pull requests and issues still open. Later polls relay every change, a merge or a
close included, made after the first run began.

A cost to keep in mind (``per_minute``): one list of each a poll, a page per 50 updated, then about
two requests per pull request updated and one or two per open one awaiting its checks; GitHub
allows 5,000 an hour per token. ``python github_example.py measure`` (for a ``[[collect]]`` entry) prints
``github_issues_open`` and ``github_prs_open{review}`` (``awaiting_review``, ``approved``,
``changes_requested``: each open pull request followed, by its reviewers' latest verdicts).

It keeps metadata only: numbers, branch names, logins, label names, review states, check names and
conclusions, times. Never a title, a body, a comment, a review's text or a check's output."""

from __future__ import annotations

import fnmatch
import math
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from orchestra.config import parse_duration

from orchestra_bridges.kits import runner
from orchestra_bridges.kits.poller import actor_source, start_of

HERE = Path(__file__).resolve().parent
HEADERS = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
PAGE = 50
OK = {"success", "neutral", "skipped"}
REVIEWS = ("awaiting_review", "approved", "changes_requested")
EVENTS = ("labeled", "unlabeled", "closed", "reopened", "merged", "review_requested")
VERDICTS = {"approved": "github.pr.approved", "changes_requested": "github.pr.changes_requested",
            "commented": "github.pr.commented"}     # a review's state, as the event type (a dismissed one: none)


def iso(t: float) -> str:
    return datetime.fromtimestamp(t, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _who(u) -> dict:
    """A person or a bot, by login and type only (never a name or an email)."""
    return {"login": u.get("login"), "type": u.get("type")} if isinstance(u, dict) else {"login": None, "type": None}


def actor(who: dict) -> str:
    login = who.get("login")
    return actor_source("github", login.removesuffix("[bot]") if login else None,
                        bot=who.get("type") == "Bot" or bool(login and login.endswith("[bot]")))


def keep_pulls(body: list) -> list[dict]:
    return [{"number": p["number"], "branch": (p.get("head") or {}).get("ref"), "head": (p.get("head") or {}).get("sha"),
             "author": _who(p.get("user")), "labels": [x.get("name") for x in p.get("labels") or []],
             "created": p.get("created_at"), "updated": p.get("updated_at"), "merged": p.get("merged_at"),
             "closed": p.get("closed_at")} for p in body or []]


def keep_issues(body: list) -> list[dict]:
    return [{"number": i["number"], "author": _who(i.get("user")), "created": i.get("created_at"),
             "updated": i.get("updated_at"), "closed": i.get("closed_at"), "pull": "pull_request" in i}
            for i in body or []]


def keep_events(body: list) -> list[dict]:
    return [{"id": e["id"], "event": e.get("event"), "label": (e.get("label") or {}).get("name"),
             "actor": _who(e.get("actor")), "at": e.get("created_at"),
             "reviewer": (e.get("requested_reviewer") or {}).get("login")}
            for e in body or [] if e.get("event") in EVENTS]


def keep_reviews(body: list) -> list[dict]:
    return [{"id": r["id"], "state": str(r.get("state") or "").lower(), "commit": r.get("commit_id"),
             "user": _who(r.get("user")), "at": r.get("submitted_at")}
            for r in body or [] if r.get("state") and r.get("state") != "PENDING"]


def keep_runs(body: dict) -> list[dict]:
    return [{"name": c.get("name"), "status": c.get("status"), "conclusion": c.get("conclusion"),
             "app": (c.get("app") or {}).get("slug"), "at": c.get("completed_at")}
            for c in (body or {}).get("check_runs") or []]


def keep_status(body: dict) -> dict:
    return {"state": (body or {}).get("state"),
            "statuses": [{"context": s.get("context"), "state": s.get("state"), "by": _who(s.get("creator")),
                          "at": s.get("updated_at")} for s in (body or {}).get("statuses") or []]}


def listed(api, path: str, params: dict, keep, after: str | None = None) -> list[dict]:
    """Every page of a list, or (``after``) the rows updated at or after it, from a list sorted
    newest first: it stops at the first page holding an older one."""
    out, page = [], 1
    while True:
        rows = api.get(path, {**params, "per_page": PAGE, "page": page}, keep=keep, ttl_s=0)
        fresh = [r for r in rows if after is None or (r["updated"] or "") >= after]
        out += fresh
        if len(rows) < PAGE or len(fresh) < len(rows):
            return out
        page += 1


def follows(settings, pr: dict) -> bool:
    branches, labels = settings.extra.get("branches") or [], settings.extra.get("labels") or []
    if not branches and not labels:
        return True
    return any(fnmatch.fnmatchcase(pr["branch"] or "", g) for g in branches) or bool(set(pr["labels"]) & set(labels))


def checks(api, repo: str, sha: str) -> dict | None:
    """The head's checks once final: outcome, via, checks, source, time; None while any still runs."""
    runs = api.get(f"/repos/{repo}/commits/{sha}/check-runs", {"per_page": 100}, keep=keep_runs, ttl_s=0)
    if runs:
        if any(r["status"] != "completed" for r in runs):
            return None
        app = next((r["app"] for r in runs if r["app"]), None)
        return {"outcome": "success" if all(r["conclusion"] in OK for r in runs) else "failure", "via": "check_run",
                "checks": sorted({r["name"] for r in runs if r["name"]}),
                "source": actor_source("github", app, bot=True) if app else "bot.github",
                "at": max((r["at"] for r in runs if r["at"]), default=None)}
    st = api.get(f"/repos/{repo}/commits/{sha}/status", keep=keep_status, ttl_s=0)
    if not st["statuses"] or st["state"] not in ("success", "failure", "error"):
        return None
    last = max(st["statuses"], key=lambda s: s["at"] or "")
    return {"outcome": "success" if st["state"] == "success" else "failure", "via": "status",
            "checks": sorted({s["context"] for s in st["statuses"] if s["context"]}), "source": actor(last["by"]),
            "at": last["at"]}


def relay_events(p, api, repo: str, n: int, kind: str, began: str, merged: bool = False) -> None:
    base = {"repository": repo, "number": n}
    for e in api.get(f"/repos/{repo}/issues/{n}/events", {"per_page": 100}, keep=keep_events, ttl_s=0):
        if (e["at"] or "") < began or (kind == "issue" and e["event"] in ("merged", "review_requested")):
            continue
        if e["event"] == "closed" and merged:
            continue                    # a merge closes too: github.pr.merged says it
        data = ({**base, "label": e["label"]} if e["event"] in ("labeled", "unlabeled") else
                {**base, "reviewer": e["reviewer"]} if e["event"] == "review_requested" else base)
        p.emit(f"github.{kind}.{e['event']}", f"{repo}#{n}", data, source=actor(e["actor"]), stamp=str(e["id"]),
               time=e["at"])


def relay_pr(p, api, repo: str, pr: dict, began: str, heads: dict) -> None:
    n, subject = pr["number"], f"{repo}#{pr['number']}"
    base, author = {"repository": repo, "number": n}, actor(pr["author"])
    if (pr["created"] or "") >= began:
        p.emit("github.pr.opened", subject, {**base, "branch": pr["branch"], "head_sha": pr["head"]}, source=author,
               stamp="opened", time=pr["created"])
    before = heads.get(str(n))
    if before and before != pr["head"]:
        p.emit("github.pr.synchronize", subject, {**base, "head_sha": pr["head"], "previous": before}, source=author,
               stamp=pr["head"])
    heads[str(n)] = pr["head"]
    relay_events(p, api, repo, n, "pr", began, merged=bool(pr["merged"]))
    for r in api.get(f"/repos/{repo}/pulls/{n}/reviews", {"per_page": 100}, keep=keep_reviews, ttl_s=0):
        if (r["at"] or "") >= began and r["state"] in VERDICTS:
            p.emit(VERDICTS[r["state"]], subject, {**base, "state": r["state"], "commit_id": r["commit"]},
                   source=actor(r["user"]), stamp=str(r["id"]), time=r["at"])


def relay_checks(p, api, repo: str, n: int, sha: str, began: str) -> bool:
    """``github.checks.completed`` for the head once its checks are final (none for checks that were
    final before ``began``); whether they are."""
    c = checks(api, repo, sha)
    if c is None:
        return False
    if c["at"] and c["at"] < began:
        return True                     # done before the poller began: not news
    p.emit("github.checks.completed", f"{repo}#{n}", {"repository": repo, "number": n, "head_sha": sha,
                                                      "outcome": c["outcome"], "via": c["via"], "checks": c["checks"]},
           source=c["source"], key=f"{repo}@{sha}", stamp="checks", time=c["at"])
    return True


def make_poll(api, settings):
    repo = str(settings.extra.get("repo", "acme/service"))
    overlap = parse_duration(settings.extra.get("overlap"), 600.0)

    def poll(p):
        started = time.time()
        cur = p.cursor if isinstance(p.cursor, dict) else {}
        # No cursor yet: the first run reads from [settings] since, never history; it relays what it
        # keeps of that window (the open items), and later polls only what was done after it began
        # (``live``), so an item the first run passed over (closed) is never relayed from before.
        first = not cur.get("since")
        since = float(cur.get("since") or math.floor(start_of(settings.extra.get("since"), started)))
        live = float(cur.get("live") or cur.get("began") or since)
        began = iso(since if first else live)
        after = iso(since - overlap)
        heads, open_, checked = dict(cur.get("heads") or {}), dict(cur.get("open") or {}), dict(cur.get("checked") or {})
        updated = listed(api, f"/repos/{repo}/pulls", {"state": "all", "sort": "updated", "direction": "desc"},
                         keep_pulls, after)
        for pr in updated:
            if not follows(settings, pr) or (first and pr["closed"]):
                continue                                 # a catch-up relays the work still open
            relay_pr(p, api, repo, pr, began, heads)
            if pr["closed"]:
                open_.pop(str(pr["number"]), None)
            else:
                open_[str(pr["number"])] = pr["head"]
        for n, sha in open_.items():                     # checks change no pull request's updated time
            if checked.get(n) != sha and relay_checks(p, api, repo, int(n), sha, began):
                checked[n] = sha
        if settings.extra.get("issues", True):
            for i in listed(api, f"/repos/{repo}/issues", {"state": "all", "since": after, "sort": "updated",
                                                           "direction": "desc"}, keep_issues, after):
                if i["pull"] or (first and i["closed"]):
                    continue
                if (i["created"] or "") >= began:
                    p.emit("github.issue.opened", f"{repo}#{i['number']}", {"repository": repo, "number": i["number"]},
                           source=actor(i["author"]), stamp="opened", time=i["created"])
                relay_events(p, api, repo, i["number"], "issue", began)
        p.cursor = {"since": started, "live": math.floor(started) if first else live, "heads": heads, "open": open_,
                    "checked": {n: s for n, s in checked.items() if n in open_}}

    return poll


def verdict(reviews: list[dict]) -> str:
    """A pull request's review state from each reviewer's latest approval or request for changes."""
    latest: dict = {}
    for r in sorted(reviews, key=lambda r: r["at"] or ""):
        if r["state"] in ("approved", "changes_requested"):
            latest[r["user"]["login"]] = r["state"]
    states = set(latest.values())
    return "changes_requested" if "changes_requested" in states else "approved" if states else "awaiting_review"


def measure(api, settings, series) -> None:
    repo = str(settings.extra.get("repo", "acme/service"))
    issues = listed(api, f"/repos/{repo}/issues", {"state": "open"}, keep_issues)
    series.gauge("github_issues_open", sum(1 for i in issues if not i["pull"]), help="Open issues (not pull requests).")
    counts = dict.fromkeys(REVIEWS, 0)
    for pr in listed(api, f"/repos/{repo}/pulls", {"state": "open"}, keep_pulls):
        if follows(settings, pr):
            reviews = api.get(f"/repos/{repo}/pulls/{pr['number']}/reviews", {"per_page": 100}, keep=keep_reviews)
            counts[verdict(reviews)] += 1
    for review, n in counts.items():
        series.gauge("github_prs_open", n, {"review": review}, help="Open pull requests followed, by review state.")


def main(argv: list[str] | None = None) -> int:
    return runner.main(HERE, make_poll, argv, measure=measure, headers=HEADERS)


if __name__ == "__main__":
    sys.exit(main())
