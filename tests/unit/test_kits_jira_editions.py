"""The Jira kit against both editions of the fake: Data Center (REST v2, ``/search`` paged by
``startAt``, a personal access token as Bearer, people by user name) and Cloud (REST v3,
``/search/jql`` paged by ``nextPageToken``, HTTP Basic, account ids). The same world and the same
changes give the same events on the bus; each edition is read in its own form, by GET only; the
``edition`` command tells them apart by ``serverInfo``."""

import importlib.util
import json
from types import SimpleNamespace

import pytest

from orchestra_bridges.kits.fakes import CANARY, FakeJira
from orchestra_bridges.kits.runner import FAKE_TOKEN, FAKE_USER
from orchestra_bridges.kits.scaffold import scaffold
from orchestra_bridges.kits.testing import set_args

EDITIONS = ("datacenter", "cloud")
SCOPE = set_args(jql="project = ABC", since="now", users=[], sprint_field="customfield_10101")


@pytest.fixture
def jira_poll(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCHESTRATOR_HOME", str(tmp_path / "home"))
    d = scaffold("jira", "jira_poll", tmp_path / "root")
    spec = importlib.util.spec_from_file_location("jira_poll", d / "jira_poll.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.PAGE = 1                                      # every search pages, so each edition's paging is used
    return mod


def run(mod, fake, tmp, edition, *extra):
    log = tmp / f"{edition}.jsonl"
    rc = mod.main([*extra, "--once", "--base-url", fake.url, "--state-dir", str(tmp / f"state-{edition}"),
                   "--events-file", str(log), "--ttl", "0", *SCOPE, "--set", f'edition="{edition}"'])
    assert rc == 0
    return [json.loads(x) for x in log.read_text().splitlines()] if log.exists() else []


def world_changes(fake):
    sprint = {"id": 7, "name": "Sprint 7", "state": "ACTIVE"}
    fake.move("ABC-1", "In Progress", by="alice")
    fake.assign("ABC-1", "bob", by="alice")
    fake.create("ABC-9", by="carol", assignee="alice", issuetype="Bug", labels=["prr"], sprint=sprint)
    fake.comment("ABC-1", by="carol")
    fake.move("ABC-9", "Done", by="bob")


def bus(events):
    """What the owner's model sees of each event: everything but the time it happened."""
    return sorted(json.dumps({k: v for k, v in e.items() if k != "time"}, sort_keys=True) for e in events)


def test_both_editions_publish_identical_events(jira_poll, tmp_path):
    got = {}
    for edition in EDITIONS:
        with FakeJira(token=FAKE_TOKEN, basic_user=FAKE_USER, edition=edition) as fake:
            assert run(jira_poll, fake, tmp_path, edition) == []          # the first run starts now
            world_changes(fake)
            got[edition] = run(jira_poll, fake, tmp_path, edition)
            assert fake.requests and all(r.method == "GET" for r in fake.requests)   # read only: no write
            assert all(r.authorized for r in fake.requests)
    dc, cloud = got["datacenter"], got["cloud"]
    assert dc and bus(dc) == bus(cloud)
    types = [e["type"] for e in dc if e["type"] != "orchestra.machine.started"]
    assert sorted(types) == sorted(["jira.issue.transitioned", "jira.issue.assigned", "jira.issue.created",
                                    "jira.issue.assigned", "jira.comment.added", "jira.issue.transitioned"])
    assert {e["source"] for e in dc} >= {"person.jira.alice", "person.jira.bob", "person.jira.carol"}
    made = next(e for e in dc if e["type"] == "jira.issue.created")
    assert made["data"]["sprint"] == "Sprint 7" and made["data"]["category"] == "done"
    unnamed = [{**e, "data": {k: v for k, v in (e.get("data") or {}).items() if k != "name"}}
               if e.get("type") == "orchestra.machine.started" else e for e in dc + cloud]
    assert CANARY not in json.dumps(unnamed)            # a display name rides on an announcement only


def test_each_edition_is_read_in_its_own_form(jira_poll, tmp_path):
    """Data Center: ``/rest/api/2/search`` with ``startAt`` and Bearer. Cloud, unchanged:
    ``/rest/api/3/search/jql`` with ``nextPageToken`` (none on the first page) and HTTP Basic."""
    with FakeJira(token=FAKE_TOKEN, edition="datacenter") as dc:
        run(jira_poll, dc, tmp_path, "datacenter")
        world_changes(dc)
        run(jira_poll, dc, tmp_path, "datacenter")
        searched = [r for r in dc.requests if "search" in r.path]
        assert {r.path for r in searched} == {"/rest/api/2/search"}
        assert searched[0].query["startAt"] == ["0"] and any(r.query["startAt"] == ["1"] for r in searched)
        assert all("nextPageToken" not in r.query and r.query["maxResults"] == ["1"] for r in searched)
        assert dc.auth_format == "Bearer {token}"
    with FakeJira(token=FAKE_TOKEN, basic_user=FAKE_USER, edition="cloud") as cloud:
        run(jira_poll, cloud, tmp_path, "cloud")
        world_changes(cloud)
        run(jira_poll, cloud, tmp_path, "cloud")
        searched = [r for r in cloud.requests if "search" in r.path]
        assert {r.path for r in searched} == {"/rest/api/3/search/jql"}
        assert all("startAt" not in r.query for r in searched)
        assert any("nextPageToken" in r.query for r in searched)          # paged by the token
        assert "nextPageToken" not in searched[0].query
        assert all(r.query["expand"] == ["changelog"] for r in searched)
        assert cloud.auth_format == "Basic {basic}"


def test_the_edition_setting(jira_poll):
    def of(base_url="https://jira.corp.example", **extra):
        return jira_poll.edition(SimpleNamespace(base_url=base_url, extra=extra))
    assert of() == "cloud"                                              # not set: cloud, as before
    assert of(edition="datacenter") == "datacenter" and of(flavour="datacenter") == "datacenter"   # the older name
    assert of(edition="auto") == "datacenter" and of("https://acme.atlassian.net", edition="auto") == "cloud"
    assert jira_poll.api_of(SimpleNamespace(base_url="", extra={})) == "/rest/api/3"
    assert jira_poll.auth_format(SimpleNamespace(base_url="", extra={"edition": "datacenter"})) == "Bearer {token}"
    with pytest.raises(ValueError):
        of(edition="server")


@pytest.mark.parametrize("edition,said", [("datacenter", "datacenter"), ("cloud", "cloud")])
def test_the_edition_command_reads_server_info(jira_poll, tmp_path, capsys, edition, said):
    with FakeJira(token=FAKE_TOKEN, basic_user=FAKE_USER, edition=edition) as fake:
        rc = jira_poll.main(["edition", "--base-url", fake.url, "--state-dir", str(tmp_path / "s"),
                             "--set", f'edition="{edition}"'])
        assert rc == 0 and [r.path for r in fake.requests] == ["/rest/api/2/serverInfo"]
    out = capsys.readouterr().out
    assert out.startswith(f'edition = "{said}"') and CANARY not in out


def test_a_data_center_site_may_say_datacenter(jira_poll, tmp_path, capsys):
    with FakeJira(token=FAKE_TOKEN, edition="datacenter", deployment="DataCenter") as fake:
        assert jira_poll.main(["edition", "--base-url", fake.url, "--state-dir", str(tmp_path / "s"),
                               "--set", 'edition="datacenter"']) == 0
    assert 'edition = "datacenter"' in capsys.readouterr().out
