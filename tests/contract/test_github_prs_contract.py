"""The pull-request fake against the real API (opt in: ``ORCHESTRA_BRIDGES_CONTRACT=1``, with a ``gh``
login). Shapes only, never content: every key the fake serves must be one the real API serves, the
fields the bridge reads must have the same types, and the translation must read the real objects.
A real key the fake lacks is printed, not failed. About a dozen requests, read-only, against
``ORCHESTRA_BRIDGES_CONTRACT_PRS_REPO`` (default microsoft/vscode: busy, with reviews and checks)."""

import os
import urllib.parse

import pytest
from fake_github_prs import FakePRs

from orchestra_bridges.github.api import GitHub, gh_token
from orchestra_bridges.github_prs.levels import PullState
from orchestra_bridges.github_prs.translate import check_run, commented, from_issue_event, pull, reviewed, status

pytestmark = pytest.mark.skipif(os.environ.get("ORCHESTRA_BRIDGES_CONTRACT") != "1",
                                reason="set ORCHESTRA_BRIDGES_CONTRACT=1 to call the real GitHub API")
REPO = os.environ.get("ORCHESTRA_BRIDGES_CONTRACT_PRS_REPO", "microsoft/vscode")
READ = {"pull": {"number": int, "created_at": str, "updated_at": str, "user": dict, "head": dict, "base": dict,
                 "labels": list, "requested_reviewers": list, "requested_teams": list, "draft": bool, "state": str},
        "reviewed": {"id": int, "user": dict, "state": str, "submitted_at": str, "commit_id": str},
        "labeled": {"id": int, "actor": dict, "created_at": str, "label": dict},
        "committed": {"sha": str, "committer": dict},
        "commented": {"id": int, "user": dict, "created_at": str},
        "line comment": {"id": int, "pull_request_review_id": int, "path": str, "commit_id": str},
        "commit": {"sha": str, "commit": dict},
        "status": {"id": int, "state": str, "context": str, "created_at": str, "creator": dict},
        "check run": {"id": int, "name": str, "status": str, "head_sha": str, "app": dict}}


def real(path, **params):
    api = GitHub(gh_token())
    body, _ = api._get(f"{api.base}{path}?{urllib.parse.urlencode({**params, 'per_page': 30})}")
    return body


def compare(name, fake_items, real_items):
    if not real_items:
        pytest.skip(f"the real API returned no {name}s to compare")
    invented = set().union(*map(set, fake_items)) - set().union(*map(set, real_items))
    assert not invented, f"the fake's {name} has keys the real API's has not: {sorted(invented)}"
    missing = set().union(*map(set, real_items)) - set().union(*map(set, fake_items))
    if missing:
        print(f"{name}: real keys the fake does not serve: {sorted(missing)}")
    for field, typ in READ[name].items():
        assert all(isinstance(i[field], typ) for i in real_items if i.get(field) is not None), (name, field)


@pytest.fixture(scope="module")
def fake():
    g = FakePRs(REPO)
    g.open(1, "alice", 1000.0, branch="b", committer="alice")
    g.label(1, "bug", "alice", 1010.0)
    g.request_review(1, "alice", "bob", 1020.0)
    g.review(1, "bob", 1030.0, "COMMENTED", comments=(("a.py", 1),))
    g.comment(1, "carol", 1040.0)
    g.check(1, "build", "success", 1050.0)
    g.status(1, "ci", "success", "dave", 1060.0)
    return g


@pytest.fixture(scope="module")
def world():
    """Recently updated closed pull requests with a review, and the first one's reads."""
    pulls = real(f"/repos/{REPO}/pulls", state="closed", sort="updated", direction="desc")
    for p in pulls[:10]:
        tl = real(f"/repos/{REPO}/issues/{p['number']}/timeline")
        if {"reviewed", "committed", "commented"} <= {e.get("event") for e in tl}:
            return pulls, p, tl
    pytest.skip("no recent pull request with a review")


def test_pulls(fake, world):
    pulls, _, _ = world
    compare("pull", list(fake.pulls.values()), pulls)
    for p in pulls:
        pull(REPO, p)


def test_timeline(fake, world):
    _, p, tl = world
    for kind in ("reviewed", "labeled", "committed", "commented"):
        got = [e for e in tl if e.get("event") == kind]
        if got:
            compare(kind, [e for e in fake.timelines[1] if e.get("event") == kind], got)
        else:
            print(f"no {kind} entry on #{p['number']} to compare")
    pr = pull(REPO, p)
    for e in tl:
        if e.get("event") == "reviewed":
            assert reviewed(pr, e, [])
        elif e.get("event") == "commented":
            assert commented(pr, e)
        else:
            from_issue_event(pr, e)
    PullState.read(pr, tl, [], [])


def test_line_comments(fake):
    """The repository's list: the same objects as a pull request's, and never empty on a busy one."""
    compare("line comment", fake.line_comments[1], real(f"/repos/{REPO}/pulls/comments", sort="created", direction="desc"))


def test_commit_checks_and_statuses(fake, world):
    _, p, _ = world
    sha = p["head"]["sha"]
    compare("commit", list(fake.commits.values()), [real(f"/repos/{REPO}/commits/{sha}")])
    pr = pull(REPO, p)
    runs = real(f"/repos/{REPO}/commits/{sha}/check-runs")["check_runs"]
    statuses = real(f"/repos/{REPO}/commits/{sha}/statuses")
    if runs:
        compare("check run", [r for rs in fake.runs.values() for r in rs], runs)
        for r in runs:
            check_run(pr, r)
    if statuses:
        compare("status", [s for ss in fake.statuses.values() for s in ss], statuses)
        for s in statuses:
            status(pr, s, sha)
