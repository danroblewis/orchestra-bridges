"""The pull-request bridge end to end on a real GitHub repository (opt in:
``ORCHESTRA_BRIDGES_SANDBOX=1``, with a ``gh`` login that can push to the sandbox).

The sandbox is ``danroblewis/orchestra-e2e-sandbox`` (``ORCHESTRA_BRIDGES_SANDBOX_REPO`` to change
it): a private repository of throwaway content made for this. The test acts there with ``gh`` and
``git`` as the owner, as a person and a pipeline would: it opens a pull request from a new branch,
reviews it with a line comment (requesting changes when GitHub allows it: it refuses that on one's
own pull request, and then the review only comments), adds and removes a label standing in for SAIL,
reports a commit status standing in for the pipeline's result, pushes a new commit and merges. The
bridge, polling the real API, must publish each of these once, as the owner, on the pull request.
Then a restart after a simulated gap (the instrument's last good poll set back) must count the gap
and publish nothing twice.

The bridge only reads; everything written to GitHub here is the test's own doing."""

import json
import os
import shutil
import subprocess
import threading
import time
import uuid

import pytest
from orchestra.config import load_project
from orchestra.events import Events
from orchestra.metrics import Point, ProjectStore, for_project

from orchestra_bridges import config as cfg
from orchestra_bridges.github.api import GitHub, gh_token
from orchestra_bridges.github_prs.bridge import INSTRUMENT, Bridge
from orchestra_bridges.github_prs.levels import LAST_POLL

pytestmark = pytest.mark.skipif(os.environ.get("ORCHESTRA_BRIDGES_SANDBOX") != "1" or not shutil.which("gh"),
                                reason="set ORCHESTRA_BRIDGES_SANDBOX=1 (and log in with gh) to drive the real sandbox")
REPO = os.environ.get("ORCHESTRA_BRIDGES_SANDBOX_REPO", "danroblewis/orchestra-e2e-sandbox")
LABEL = "e2e-sail"
STATUS = "e2e/sail"


def sh(*argv, cwd=None, check=True) -> subprocess.CompletedProcess:
    return subprocess.run(argv, cwd=cwd, capture_output=True, text=True, timeout=120, check=check)


def gh_api(*args, check=True) -> subprocess.CompletedProcess:
    return sh("gh", "api", *args, check=check)


@pytest.fixture
def project(tmp_path, monkeypatch):
    run = f"e2e/{time.strftime('%Y%m%d-%H%M%S')}-{uuid.uuid4().hex[:6]}"
    monkeypatch.setenv("ORCHESTRATOR_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("ORCHESTRA_EVENTS_URL", raising=False)
    monkeypatch.delenv("ORCHESTRA_METRICS_URL", raising=False)
    root = tmp_path / "project"
    root.mkdir()
    sh("git", "init", "-q", "-b", "main", cwd=root)
    (root / "orchestra.toml").write_text(f'[bridges.github_prs]\nrepos = ["{REPO}"]\nbranches = ["{run}"]\n'
                                         'every = "5s"\nlookback = "10m"\noverlap = "2m"\nport = 0\n')
    return load_project(root), run


def make(project) -> Bridge:
    conf = cfg.github_prs(project.config.raw, project.repo_root)
    return Bridge(conf, GitHub(gh_token(conf.api), conf.api), Events.for_project(project),
                  ProjectStore(for_project(project), project.key))


def pr_events(project) -> list[dict]:
    return [e for e in Events.for_project(project).after(0)[0] if e["type"].startswith("github.pr.")]


def poll_until(bridge, project, done, timeout=90.0) -> list[dict]:
    """Poll every few seconds until ``done(events)``: GitHub's lists catch up a few seconds late."""
    end = time.time() + timeout
    while True:
        bridge.round()
        evs = pr_events(project)
        if done(evs) or time.time() > end:
            return evs
        time.sleep(4)


def types(evs) -> list[str]:
    return [e["type"] for e in evs]


def test_a_pull_request_from_opening_to_merge(project, tmp_path):
    project, branch = project
    me = sh("gh", "api", "user", "--jq", ".login").stdout.strip()
    clone = tmp_path / "clone"
    sh("gh", "repo", "clone", REPO, str(clone), "--", "-q")
    git = ("git", "-c", "user.name=Orchestra e2e", "-c", "user.email=orchestra-e2e@users.noreply.github.com")
    sh("git", "checkout", "-q", "-b", branch, cwd=clone)
    (clone / "runs").mkdir(exist_ok=True)
    note = clone / "runs" / f"{branch.split('/')[-1]}.txt"
    note.write_text("first\n")
    sh("git", "add", ".", cwd=clone)
    sh(*git, "commit", "-qm", "e2e: a throwaway change", cwd=clone)
    sh("git", "push", "-q", "origin", branch, cwd=clone)
    sh("gh", "label", "create", LABEL, "--repo", REPO, "--color", "ededed", "--force")

    bridge = make(project)
    bridge.resume()
    first_poll = time.time()
    bridge.round()                                                   # nothing of this run yet
    assert pr_events(project) == []

    url = sh("gh", "pr", "create", "--repo", REPO, "--head", branch, "--base", "main", "--title", "e2e",
             "--body", "A throwaway pull request made by the e2e test.").stdout.strip()
    n = int(url.rstrip("/").rsplit("/", 1)[-1])
    try:
        head1 = sh("git", "rev-parse", "HEAD", cwd=clone).stdout.strip()
        review = {"commit_id": head1, "body": "e2e review", "event": "REQUEST_CHANGES",
                  "comments": [{"path": f"runs/{note.name}", "line": 1, "side": "RIGHT", "body": "e2e line comment"}]}
        refused = subprocess.run(["gh", "api", f"repos/{REPO}/pulls/{n}/reviews", "--input", "-"],
                                 input=json.dumps(review), capture_output=True, text=True, timeout=60, check=False)
        expected_state = "changes_requested"
        if refused.returncode != 0:                                  # one's own pull request: GitHub refuses
            print(f"REQUEST_CHANGES refused by GitHub: {refused.stdout.strip()[:200]}")
            expected_state = "commented"
            subprocess.run(["gh", "api", f"repos/{REPO}/pulls/{n}/reviews", "--input", "-"],
                           input=json.dumps({**review, "event": "COMMENT"}), capture_output=True, text=True,
                           timeout=60, check=True)
        sh("gh", "pr", "edit", str(n), "--repo", REPO, "--add-label", LABEL)
        poll_until(bridge, project, lambda e: "github.pr.labelled" in types(e) and "github.pr.review_commented" in types(e))
        sh("gh", "pr", "edit", str(n), "--repo", REPO, "--remove-label", LABEL)
        gh_api(f"repos/{REPO}/statuses/{head1}", "-f", "state=failure", "-f", f"context={STATUS}",
               "-f", "description=e2e stand-in for the pipeline")
        poll_until(bridge, project, lambda e: "github.pr.unlabelled" in types(e) and "github.pr.check_completed" in types(e))
        note.write_text("first\nsecond\n")
        sh(*git, "commit", "-qam", "e2e: a second commit", cwd=clone)
        sh("git", "push", "-q", "origin", branch, cwd=clone)
        head2 = sh("git", "rev-parse", "HEAD", cwd=clone).stdout.strip()
        poll_until(bridge, project, lambda e: "github.pr.pushed" in types(e))
        sh("gh", "pr", "merge", str(n), "--repo", REPO, "--merge", "--delete-branch")
        evs = poll_until(bridge, project, lambda e: "github.pr.merged" in types(e))
    finally:
        if sh("gh", "pr", "view", str(n), "--repo", REPO, "--json", "state", "--jq", ".state", check=False).stdout.strip() == "OPEN":
            sh("gh", "pr", "close", str(n), "--repo", REPO, "--delete-branch", check=False)

    subject = f"{REPO}#{n}"
    print("published:", [(e["type"], e["source"], e["data"].get("state") or e["data"].get("label") or "") for e in evs])
    want = ["github.pr.opened", "github.pr.review_submitted", "github.pr.review_commented", "github.pr.labelled",
            "github.pr.unlabelled", "github.pr.check_completed", "github.pr.pushed", "github.pr.merged"]
    assert sorted(types(evs)) == sorted(want), types(evs)          # each once, and no close for the merge
    assert all(e["subject"] == subject and e["source"] == f"person.{me.lower()}" for e in evs)
    assert all(e["data"]["branch"] == branch and e["data"]["pull"] == n for e in evs)
    by = {e["type"]: e for e in evs}
    assert by["github.pr.review_submitted"]["data"]["state"] == expected_state
    assert by["github.pr.review_submitted"]["data"]["comments"] == 1
    assert by["github.pr.review_commented"]["data"]["path"] == f"runs/{note.name}"
    assert by["github.pr.labelled"]["data"]["label"] == LABEL == by["github.pr.unlabelled"]["data"]["label"]
    assert by["github.pr.check_completed"]["data"]["conclusion"] == "failure"
    assert (by["github.pr.pushed"]["data"]["previous"], by["github.pr.pushed"]["data"]["head"]) == (head1, head2)
    assert by["github.pr.opened"]["data"]["head"] == head1
    raw = json.dumps(Events.for_project(project).after(0)[0])
    assert "e2e line comment" not in raw and "e2e review" not in raw and "throwaway pull request" not in raw

    # A restart after a gap: the instrument's last good poll was before all of this.
    ProjectStore(for_project(project), project.key).write(
        [Point(LAST_POLL, first_poll, first_poll, {"instrument": INSTRUMENT, "repo": REPO})])
    again = make(project)
    again.run(threading.Event(), once=True)
    assert tuple(again.gaps.values(REPO).values())[:2] == (1, 1)     # one gap, read back whole: counted, not published
    assert [e["id"] for e in pr_events(project)] == [e["id"] for e in evs]
