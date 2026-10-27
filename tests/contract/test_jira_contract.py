"""The fake Jira against a real one (opt in: ``ORCHESTRA_BRIDGES_JIRA_CONTRACT=1``). Shapes only,
never content: every key the fake serves must be one the real API serves, the fields the bridge
reads must have the same types, and the records and events must read real objects. A real key the
fake lacks is printed, not failed. Read-only; a few requests. Not run yet: there is no account.

    ORCHESTRA_BRIDGES_JIRA_CONTRACT=1
    JIRA_URL=https://jira.acme.com            # the Data Center base URL, or a Cloud site
    JIRA_DEPLOYMENT=datacenter                # or cloud
    JIRA_API_TOKEN=...                        # a personal access token (Data Center), or an API token (Cloud)
    JIRA_EMAIL=me@acme.com                    # Cloud only
    JIRA_CONTRACT_JQL='project = ABC'         # issues to read; the test adds "updated >= -30d"
"""

import os

import pytest
from fake_jira import FakeJira

from orchestra_bridges.jira import poll
from orchestra_bridges.jira.api import Jira
from orchestra_bridges.jira.records import issue
from orchestra_bridges.jira.translate import events

pytestmark = pytest.mark.skipif(os.environ.get("ORCHESTRA_BRIDGES_JIRA_CONTRACT") != "1",
                                reason="set ORCHESTRA_BRIDGES_JIRA_CONTRACT=1 (and JIRA_URL, JIRA_API_TOKEN) to call a real Jira")
DEPLOYMENT = os.environ.get("JIRA_DEPLOYMENT", "datacenter")
JQL = os.environ.get("JIRA_CONTRACT_JQL", "")
READ = {"issue": {"id": str, "key": str, "fields": dict},
        "fields": {"project": dict, "status": dict, "created": str},
        "status": {"id": str, "name": str, "statusCategory": dict},
        "history": {"id": str, "created": str, "items": list},
        "item": {"field": str},
        "comment": {"id": str, "created": str, "author": dict},
        "user": {"displayName": str, **({"accountId": str} if DEPLOYMENT == "cloud" else {"name": str, "key": str})}}


def compare(name, fake_items, real_items):
    if not real_items:
        pytest.skip(f"the real Jira returned no {name} to compare")
    invented = set().union(*map(set, fake_items)) - set().union(*map(set, real_items))
    assert not invented, f"the fake's {name} has keys the real API's has not: {sorted(invented)}"
    missing = set().union(*map(set, real_items)) - set().union(*map(set, fake_items))
    if missing:
        print(f"{name}: real keys the fake does not serve: {sorted(missing)}")
    for field, typ in READ.get(name, {}).items():
        assert all(isinstance(i[field], typ) for i in real_items if i.get(field) is not None), (name, field)


@pytest.fixture(scope="module")
def real():
    api = Jira(os.environ["JIRA_URL"], os.environ["JIRA_API_TOKEN"], deployment=DEPLOYMENT,
               email=os.environ.get("JIRA_EMAIL"))
    site = poll.site(api, None)
    scope = f"({JQL}) AND " if JQL else ""
    raws = list(api.search(f'{scope}updated >= "-43200m" ORDER BY updated DESC', poll.UPDATED_FIELDS
                           + ([site.sprint_field] if site.sprint_field else []), changelog=True, page=20))[:20]
    return api, site, raws


@pytest.fixture(scope="module")
def fake():
    f = FakeJira(DEPLOYMENT)
    key = f.create("ABC", "alice", 1000.0, assignee="bob", labels=("x",))
    f.transition(key, "bob", 1100.0, "3")
    f.assign(key, "bob", 1200.0, "carol")
    f.comment(key, "carol", 1300.0)
    f.link(key, "carol", 1400.0, "ABC-1")
    f.sprint(key, "carol", 1500.0, 12)
    raw = f.issue_json(f.issues[key], poll.UPDATED_FIELDS + [f.sprint_field], True)
    return f, raw


def test_issues_fields_changelogs_and_comments_have_the_real_shapes(real, fake):
    raws = real[2]
    f, raw = fake
    compare("issue", [raw], raws)
    compare("fields", [raw["fields"]], [r["fields"] for r in raws])
    compare("status", [raw["fields"]["status"]], [r["fields"]["status"] for r in raws])
    histories = [h for r in raws for h in (r.get("changelog") or {}).get("histories") or []]
    compare("history", raw["changelog"]["histories"], histories)
    compare("item", [i for h in raw["changelog"]["histories"] for i in h["items"]],
            [i for h in histories for i in h.get("items") or []])
    comments = [c for r in raws for c in ((r["fields"].get("comment") or {}).get("comments") or [])]
    compare("comment", raw["fields"]["comment"]["comments"], comments)
    users = [u for r in raws for u in (r["fields"].get("creator"), r["fields"].get("assignee")) if u]
    compare("user", [f.user("alice")], users)


def test_the_bridge_reads_real_objects(real):
    api, site, raws = real
    assert site.me.login and site.statuses
    for r in raws:
        rec = issue(r, api.cloud, site.sprint_field, (r.get("changelog") or {}).get("histories"),
                    (r["fields"].get("comment") or {}).get("comments"))
        for s in events(api.base.split("//")[1], rec, site.statuses, lambda login: f"person.jira.{login}"):
            assert s.id and s.subject == rec.key and s.ts > 0
