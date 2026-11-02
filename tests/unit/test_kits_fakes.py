"""Each kit fake: its endpoints in the real shapes, its fixture, its token check, injected failures,
and the requests it recorded."""

import base64
import json
import urllib.error
import urllib.parse
import urllib.request

import pytest

from orchestra_bridges.kits.fakes import CANARY, FakeGitHub, FakeJira, FakePagerDuty, FakePrometheus, FakeSlack


def call(fake, path, params=None, *, method="GET", body=None, auth=None, form=None):
    url = fake.url + path + ("?" + urllib.parse.urlencode(params, doseq=True) if params else "")
    data, headers = None, {}
    if body is not None:
        data, headers["Content-Type"] = json.dumps(body).encode(), "application/json"
    if form is not None:
        data = urllib.parse.urlencode(form, doseq=True).encode()
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    if auth:
        headers["Authorization"] = auth
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data, headers, method=method), timeout=5) as r:
            raw = r.read()
            return r.status, json.loads(raw) if raw else None
    except urllib.error.HTTPError as e:
        raw = e.read()
        return e.code, json.loads(raw) if raw else None


def test_pagerduty():
    with FakePagerDuty(token="pd") as pd:
        assert call(pd, "/incidents")[0] == 401
        auth = "Token token=pd"
        st, body = call(pd, "/incidents", {"statuses[]": ["triggered", "acknowledged"]}, auth=auth)
        assert st == 200 and [i["id"] for i in body["incidents"]] == ["PINC001", "PINC002"]
        assert body["more"] is False and CANARY in body["incidents"][0]["title"]
        assert body["incidents"][1]["last_status_change_by"]["type"] == "user_reference"
        st, body = call(pd, "/incidents", {"limit": 1}, auth=auth)
        assert len(body["incidents"]) == 1 and body["more"] is True
        assert call(pd, "/incidents/PINC002", auth=auth)[1]["incident"]["status"] == "acknowledged"
        assert call(pd, "/incidents/NOPE", auth=auth)[0] == 404
        assert call(pd, "/incidents", {"statuses[]": ["bogus"]}, auth=auth)[0] == 400
        pd.add_incident("PINC003")
        pd.set_status("PINC001", "resolved", by="PUSER02")
        st, body = call(pd, "/incidents", {"statuses[]": ["triggered"]}, auth=auth)
        assert [i["id"] for i in body["incidents"]] == ["PINC003"]
        pd.fail_next(429, {"Retry-After": "3"})
        assert call(pd, "/incidents", auth=auth)[0] == 429
        assert pd.requests[0].authorized is False and pd.requests[1].authorized is True
        assert pd.requests[1].query["statuses[]"] == ["triggered", "acknowledged"]


def test_jira_search_issue_and_transitions():
    with FakeJira(token="pat") as j:
        auth = "Bearer pat"
        st, body = call(j, "/rest/api/2/search", {"jql": "project = ABC ORDER BY key ASC", "fields": "status"}, auth=auth)
        assert st == 200 and body["total"] == 2 and [i["key"] for i in body["issues"]] == ["ABC-1", "ABC-2"]
        assert set(body["issues"][0]["fields"]) == {"status"}
        st, body = call(j, "/rest/api/2/search", method="POST", auth=auth,
                        body={"jql": '(project in (ABC, OPS)) AND statusCategory != Done', "maxResults": 1})
        assert body["total"] == 2 and len(body["issues"]) == 1
        st, body = call(j, "/rest/api/2/search", {"jql": "project = ABC", "expand": "changelog"}, auth=auth)
        hist = body["issues"][1]["changelog"]["histories"]
        assert hist[0]["author"]["name"] == "bob" and hist[0]["items"][0]["toString"] == "In Progress"
        assert call(j, "/rest/api/2/search", {"jql": "summary ~ x"}, auth=auth)[0] == 400
        assert call(j, "/rest/api/2/issue/ABC-1", auth=auth)[1]["fields"]["status"]["name"] == "Open"
        assert call(j, "/rest/api/2/issue/ABC-99", auth=auth)[0] == 404
        st, body = call(j, "/rest/api/2/issue/ABC-1/transitions", auth=auth)
        assert [t["to"]["name"] for t in body["transitions"]] == ["In Progress"]
        assert call(j, "/rest/api/2/issue/ABC-1/transitions", method="POST", auth=auth,
                    body={"transition": {"id": "11"}})[0] == 204
        assert call(j, "/rest/api/2/issue/ABC-1", auth=auth)[1]["fields"]["status"]["name"] == "In Progress"
        assert call(j, "/rest/api/2/issue/ABC-1/transitions", method="POST", auth=auth,
                    body={"transition": {"id": "99"}})[0] == 400
        st, body = call(j, "/rest/api/2/search", {"jql": 'updated >= "-5m"'}, auth=auth)
        assert [i["key"] for i in body["issues"]] == ["ABC-1"]
        assert len(j.requests_to("/rest/api/2/search")) == 5


def test_slack_history_and_users():
    with FakeSlack(token="xoxb") as s:
        auth = "Bearer xoxb"
        _, body = call(s, "/api/conversations.history", {"channel": "C001", "limit": 1}, auth=auth)
        assert body["ok"] and [m["ts"] for m in body["messages"]] == ["1793000120.000300"]   # no reply: Slack's shape
        assert body["has_more"] and body["response_metadata"]["next_cursor"] == "1"
        _, body = call(s, "/api/conversations.history", {"channel": "C001", "cursor": "1"}, auth=auth)
        [parent] = body["messages"]
        assert parent["ts"] == "1793000000.000100" and not body["has_more"]
        assert parent["reply_count"] == 1 and parent["reply_users"] == ["U002"] and parent["latest_reply"] == "1793000060.000200"
        _, body = call(s, "/api/conversations.history", {"channel": "C001", "oldest": "1793000000.000100"}, auth=auth)
        assert len(body["messages"]) == 1                                   # oldest is exclusive
        assert call(s, "/api/conversations.history", {"channel": "C404"}, auth=auth)[1]["error"] == "channel_not_found"
        _, body = call(s, "/api/users.info", form={"user": "B001"}, method="POST", auth=auth)
        assert body["ok"] and body["user"]["is_bot"] is True
        assert call(s, "/api/users.info", {"user": "U999"}, auth=auth)[1] == {"ok": False, "error": "user_not_found"}
        s.post("C001", "U001")
        assert len(call(s, "/api/conversations.history", {"channel": "C001"}, auth=auth)[1]["messages"]) == 3
        assert CANARY in json.dumps(call(s, "/api/conversations.history", {"channel": "C001"}, auth=auth)[1])
    with FakeSlack(replies_in_history=True) as s:                         # the older fake's shape
        assert len(call(s, "/api/conversations.history", {"channel": "C001"})[1]["messages"]) == 3


def test_prometheus_endpoints():
    with FakePrometheus(t0=1000.0) as p:
        st, body = call(p, "/api/v1/query", form={"query": 'queue_depth{queue="orders"}', "time": "1000"}, method="POST")
        assert st == 200 and body["data"]["resultType"] == "vector"
        assert body["data"]["result"][0]["value"] == [1000.0, "5"]
        st, body = call(p, "/api/v1/query_range", {"query": "sum(http_requests_total)", "start": "400", "end": "1000",
                                                   "step": "300"})
        assert [v for _, v in body["data"]["result"][0]["values"]] == ["150", "215", "310"]
        st, body = call(p, "/api/v1/series", {"match[]": ['{k8s_cluster="prod-1"}']})
        assert len(body["data"]) == 2
        st, body = call(p, "/api/v1/query", {"query": "rate(x[5m])"})
        assert st == 400 and body["errorType"] == "bad_data"
        assert call(p, "/api/v1/query", {"query": 'queue_depth{queue=~"ord.*"}', "time": "2000"})[1]["data"]["result"] == []
        assert [r.path for r in p.requests][:2] == ["/api/v1/query", "/api/v1/query_range"]


def test_a_fake_can_be_seeded_from_the_tests_own_fixture(tmp_path):
    f = tmp_path / "pd.json"
    f.write_text(json.dumps({"services": [], "incidents": []}))
    with FakePagerDuty(f) as pd:
        assert call(pd, "/incidents")[1]["incidents"] == []


def test_url_before_start_raises():
    with pytest.raises(RuntimeError):
        _ = FakeSlack().url


def test_jira_cloud_searches_by_page_token_with_basic_auth_and_account_ids():
    basic = "Basic " + base64.b64encode(b"me@acme.com:tok").decode()
    with FakeJira(token="tok", basic_user="me@acme.com", flavour="cloud", me="acct-me") as j:
        assert call(j, "/rest/api/3/myself", auth="Bearer tok")[0] == 401          # Cloud is Basic
        assert call(j, "/rest/api/3/myself", auth=basic) == (200, {"accountId": "acct-me", "displayName": f"{CANARY} acct-me",
                                                                   "emailAddress": CANARY})
        assert call(j, "/rest/api/3/search", {"jql": "project = ABC"}, auth=basic)[0] == 410   # removed from Cloud
        st, page = call(j, "/rest/api/3/search/jql", {"jql": "project = ABC ORDER BY key", "maxResults": 1,
                                                     "expand": "changelog"}, auth=basic)
        assert st == 200 and [i["key"] for i in page["issues"]] == ["ABC-1"] and page["isLast"] is False
        assert "total" not in page and page["issues"][0]["fields"]["assignee"]["accountId"] == "alice"
        assert "name" not in page["issues"][0]["fields"]["assignee"]
        st, page = call(j, "/rest/api/3/search/jql", {"jql": "project = ABC ORDER BY key", "maxResults": 1,
                                                     "nextPageToken": page["nextPageToken"], "expand": "changelog"},
                        auth=basic)
        assert [i["key"] for i in page["issues"]] == ["ABC-2"] and page["isLast"] is True and "nextPageToken" not in page
        assert page["issues"][0]["changelog"]["histories"][0]["author"]["accountId"] == "bob"
        assert call(j, "/rest/api/2/search", {"jql": "project = ABC"}, auth=basic)[0] == 404   # no v2 on this fake


def test_jira_assign_is_in_the_changelog_by_account():
    with FakeJira(token="t") as j:
        j.assign("ABC-1", "bob", by="carol")
        _, body = call(j, "/rest/api/2/issue/ABC-1", {"expand": "changelog"}, auth="Bearer t")
        [item] = body["changelog"]["histories"][-1]["items"]
        assert (item["field"], item["from"], item["to"], item["toString"]) == ("assignee", "alice", "bob", CANARY)
        assert body["fields"]["assignee"]["name"] == "bob"


def test_github_reads_and_label_writes():
    with FakeGitHub(token="gh") as g:
        r = "/repos/acme/service"
        assert call(g, f"{r}/pulls")[0] == 401
        auth = "Bearer gh"
        st, pulls = call(g, f"{r}/pulls", {"state": "all", "sort": "updated", "direction": "desc"}, auth=auth)
        assert st == 200 and {p["number"] for p in pulls} == {1, 2} and CANARY in pulls[0]["title"]
        assert call(g, f"{r}/pulls", {"head": "acme:fix/timeout"}, auth=auth)[1][0]["number"] == 2
        st, issues = call(g, f"{r}/issues", {"state": "all"}, auth=auth)
        assert {i["number"]: "pull_request" in i for i in issues} == {1: True, 2: True, 3: False, 4: False}
        assert call(g, f"{r}/issues", {"state": "open", "per_page": 2, "page": 2}, auth=auth)[1][0]["number"] in (1, 2, 3)
        assert [e["event"] for e in call(g, f"{r}/issues/4/events", auth=auth)[1]] == ["closed"]
        assert call(g, f"{r}/pulls/2/reviews", auth=auth)[1][0]["state"] == "APPROVED"
        runs = call(g, f"{r}/commits/{'a1' * 20}/check-runs", auth=auth)[1]
        assert runs["check_runs"][0]["conclusion"] == "success" and CANARY in runs["check_runs"][0]["output"]["title"]
        assert call(g, f"{r}/commits/{'a1' * 20}/status", auth=auth)[1]["state"] == "pending"
        # the writes a test (or the Outbox) makes: labels added, replaced and taken off, each an event
        assert call(g, f"{r}/labels", method="POST", body={"name": "SAILED"}, auth=auth)[0] == 201
        st, labels = call(g, f"{r}/issues/2/labels", method="POST", body={"labels": ["SAIL"]}, auth=auth)
        assert st == 200 and [x["name"] for x in labels] == ["SAIL"]
        st, labels = call(g, f"{r}/issues/2/labels", method="PUT", body={"labels": ["SAILED"]}, auth=auth)
        assert [x["name"] for x in labels] == ["SAILED"]
        assert call(g, f"{r}/issues/2/labels/SAILED", method="DELETE", auth=auth)[0] == 200
        assert call(g, f"{r}/issues/2/labels/SAILED", method="DELETE", auth=auth)[0] == 404
        assert [e["event"] for e in call(g, f"{r}/issues/2/events", auth=auth)[1]] == [
            "labeled", "unlabeled", "labeled", "unlabeled"]
        # the owner cannot approve their own pull request; a comment review and a merge are fine
        assert call(g, f"{r}/pulls/1/reviews", method="POST", body={"event": "APPROVE"}, auth=auth)[0] == 422
        assert call(g, f"{r}/pulls/1/reviews", method="POST", body={"event": "COMMENT"}, auth=auth)[1]["state"] == "COMMENTED"
        assert call(g, f"{r}/pulls/1/merge", method="PUT", body={}, auth=auth)[1]["merged"] is True
        assert call(g, f"{r}/pulls/1/merge", method="PUT", body={}, auth=auth)[0] == 405
        assert call(g, f"{r}/pulls", method="POST", body={"head": "feature/x", "base": "main"}, auth=auth)[0] == 201
        assert call(g, f"{r}/pulls", method="POST", body={"head": "feature/x"}, auth=auth)[0] == 422
        assert call(g, f"{r}/git/refs/heads/feature/x", method="DELETE", auth=auth)[0] == 204
        assert call(g, "/repos/other/repo/pulls", auth=auth)[0] == 404


def test_github_fixture_times_are_recent():
    with FakeGitHub() as g:
        created = call(g, "/repos/acme/service/issues/3")[1]["created_at"]
        assert created.startswith("20") and created.endswith("Z") and not created.startswith("-")
