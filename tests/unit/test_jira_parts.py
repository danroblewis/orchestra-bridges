"""The Jira bridge's parts: the token's sources and rules, the client's limits, and the records."""


import pytest
from fake_jira import CANARY, TOKEN, FakeJira

from orchestra_bridges.jira.api import Jira, JiraError
from orchestra_bridges.jira.records import _sprints, issue
from orchestra_bridges.secret import TokenError, read_token


def test_the_token_comes_from_the_environment_or_a_private_file_outside_the_repository(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.delenv("JIRA_API_TOKEN", raising=False)
    monkeypatch.delenv("JIRA_API_TOKEN_FILE", raising=False)
    f = tmp_path / "jira-token"
    f.write_text("  from-file\n")
    f.chmod(0o644)
    with pytest.raises(TokenError, match="chmod 600"):
        read_token("JIRA_API_TOKEN", "jira-token", repo, f)
    f.chmod(0o600)
    assert read_token("JIRA_API_TOKEN", "jira-token", repo, f) == "from-file"
    inside = repo / "token"
    inside.write_text("x")
    inside.chmod(0o600)
    with pytest.raises(TokenError, match="inside the repository"):
        read_token("JIRA_API_TOKEN", "jira-token", repo, inside)
    with pytest.raises(TokenError, match="no JIRA_API_TOKEN") as e:
        read_token("JIRA_API_TOKEN", "jira-token", repo, tmp_path / "missing")
    assert "from-file" not in str(e.value)
    monkeypatch.setenv("JIRA_API_TOKEN_FILE", str(f))
    assert read_token("JIRA_API_TOKEN", "jira-token", repo) == "from-file"
    monkeypatch.setenv("JIRA_API_TOKEN", "from-env")
    assert read_token("JIRA_API_TOKEN", "jira-token", repo, inside) == "from-env"


def test_the_client_never_shows_the_token_and_names_only_the_path_and_code():
    with FakeJira() as fake:
        api = Jira(fake.base, TOKEN, sleep=lambda s: None)
        assert TOKEN not in repr(api)
        fake.fail_next = [(500, {})]
        with pytest.raises(JiraError) as e:
            api.get("/myself")
        assert str(e.value) == "GET /rest/api/2/myself: HTTP 500"
        bad = Jira(fake.base, "wrong", sleep=lambda s: None)
        with pytest.raises(JiraError, match="HTTP 401"):
            bad.get("/myself")
        fake.fail_next = [(429, {"Retry-After": "100000"})] * 4
        waited = []
        capped = Jira(fake.base, TOKEN, sleep=waited.append, max_wait=30)
        with pytest.raises(JiraError, match="HTTP 429"):
            capped.get("/myself")
        assert waited == [30.0, 30.0, 30.0]                        # capped, and given up after the retries
        fake.asleep = True
        with pytest.raises(JiraError, match="GET /rest/api/2/myself"):
            api.get("/myself")


def test_cloud_needs_the_accounts_email():
    with pytest.raises(JiraError, match="email"):
        Jira("https://acme.atlassian.net", TOKEN, deployment="cloud")


def test_sprints_in_both_shapes_keep_ids_of_sprints_not_closed():
    dc = [f"com.atlassian.greenhopper.service.sprint.Sprint@1[id=12,rapidViewId=3,state=ACTIVE,name=S,goal={CANARY}]",
          "com.atlassian.greenhopper.service.sprint.Sprint@2[id=11,rapidViewId=3,state=CLOSED,name=S,goal=]"]
    assert _sprints(dc) == (12,)
    assert _sprints([{"id": 12, "state": "active", "goal": CANARY}, {"id": 13, "state": "future"},
                     {"id": 11, "state": "closed"}]) == (12, 13)
    assert _sprints(None) == ()


def test_a_record_holds_no_text():
    fake = FakeJira()
    key = fake.create("ABC", "alice", 1000.0)
    fake.edit_text(key, "alice", 1100.0)
    fake.comment(key, "bob", 1200.0)
    fake.link(key, "bob", 1300.0, "OPS-1")
    i = fake.issues[key]
    raw = fake.issue_json(i, ["*all"], True)
    rec = issue(raw, False, "customfield_10100", raw["changelog"]["histories"], raw["fields"]["comment"]["comments"])
    assert CANARY in repr(raw) and CANARY not in repr(rec)
    assert [c.field for h in rec.histories for c in h.changes] == ["link"]       # the text change is dropped
