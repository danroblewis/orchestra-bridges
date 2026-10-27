"""The fake against the real API (opt in: ``ORCHESTRA_BRIDGES_CONTRACT=1``, with a ``gh`` login).

Only shapes are compared, never content: every key the fake serves must be one the real API serves
(the fake invents nothing), the fields the bridge reads must have the same types, and the bridge's
translation must read the real objects. GitHub adds keys from time to time; a real key the fake
lacks is printed, not failed. Six requests, read-only."""

import os
import urllib.parse

import pytest
from fake_github import FakeGitHub

from orchestra_bridges.github.api import GitHub, gh_token
from orchestra_bridges.github.translate import commented, from_event, opened

pytestmark = pytest.mark.skipif(os.environ.get("ORCHESTRA_BRIDGES_CONTRACT") != "1",
                                reason="set ORCHESTRA_BRIDGES_CONTRACT=1 to call the real GitHub API")
REPO = os.environ.get("ORCHESTRA_BRIDGES_CONTRACT_REPO", "microsoft/vscode")
READ = {"issue": {"number": int, "created_at": str, "updated_at": str, "user": dict, "author_association": str},
        "comment": {"id": int, "created_at": str, "updated_at": str, "html_url": str, "issue_url": str, "user": dict,
                    "author_association": str},
        "event": {"id": int, "event": str, "created_at": str, "issue": dict, "actor": dict},
        "user": {"login": str, "type": str}}


def real(path, **params):
    api = GitHub(gh_token())
    body, _ = api._get(f"{api.base}{path}?{urllib.parse.urlencode({**params, 'per_page': 30})}")
    return body


def keys(items):
    return set().union(*(set(i) for i in items))


def compare(name, fake_items, real_items):
    assert real_items, f"the real API returned no {name}s"
    invented = keys(fake_items) - keys(real_items)
    assert not invented, f"the fake's {name} has keys the real API's has not: {sorted(invented)}"
    missing = keys(real_items) - keys(fake_items)
    if missing:
        print(f"{name}: real keys the fake does not serve: {sorted(missing)}")
    for field, typ in READ[name].items():
        assert all(isinstance(i[field], typ) for i in real_items if i.get(field) is not None), (name, field)


@pytest.fixture(scope="module")
def world():
    g = FakeGitHub(repo=REPO)
    g.open(1, "alice", 1000.0)
    g.open(2, "bob", 1000.0, pull=True)
    g.comment(1, "carol", 1100.0)
    g.event(1, "labeled", "dave", 1200.0, label="bug")
    g.event(1, "closed", "dave", 1300.0)
    return g


def test_issues(world):
    items = real(f"/repos/{REPO}/issues", state="all", sort="updated", direction="desc")
    compare("issue", [i for i in world.issues.values() if "pull_request" not in i], [i for i in items if "pull_request" not in i])
    compare("user", [i["user"] for i in world.issues.values()], [i["user"] for i in items])
    assert all(opened(REPO, i) is not None for i in items if "pull_request" not in i)


def test_comments(world):
    items = real(f"/repos/{REPO}/issues/comments", sort="updated", direction="desc")
    compare("comment", world.comments, items)
    for c in items:
        commented(REPO, c)


def test_issue_events(world):
    items = real(f"/repos/{REPO}/issues/events")
    compare("event", world.events, items)
    for e in items:
        s = from_event(REPO, e)
        assert s is None or s.type.startswith("github.issue.")
