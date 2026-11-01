"""Each kit fake: its endpoints in the real shapes, its fixture, its token check, injected failures,
and the requests it recorded."""

import json
import urllib.error
import urllib.parse
import urllib.request

import pytest

from orchestra_bridges.kits.fakes import CANARY, FakeJira, FakePagerDuty, FakePrometheus, FakeSlack


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
        _, body = call(s, "/api/conversations.history", {"channel": "C001", "limit": 2}, auth=auth)
        assert body["ok"] and [m["ts"] for m in body["messages"]] == ["1793000120.000300", "1793000060.000200"]
        assert body["has_more"] and body["response_metadata"]["next_cursor"] == "2"
        _, body = call(s, "/api/conversations.history", {"channel": "C001", "cursor": "2"}, auth=auth)
        assert [m["ts"] for m in body["messages"]] == ["1793000000.000100"] and not body["has_more"]
        _, body = call(s, "/api/conversations.history", {"channel": "C001", "oldest": "1793000060.000200"}, auth=auth)
        assert len(body["messages"]) == 1                                   # oldest is exclusive
        assert call(s, "/api/conversations.history", {"channel": "C404"}, auth=auth)[1]["error"] == "channel_not_found"
        _, body = call(s, "/api/users.info", form={"user": "B001"}, method="POST", auth=auth)
        assert body["ok"] and body["user"]["is_bot"] is True
        assert call(s, "/api/users.info", {"user": "U999"}, auth=auth)[1] == {"ok": False, "error": "user_not_found"}
        s.post("C001", "U001")
        assert len(call(s, "/api/conversations.history", {"channel": "C001"}, auth=auth)[1]["messages"]) == 4
        assert CANARY in json.dumps(call(s, "/api/conversations.history", {"channel": "C001"}, auth=auth)[1])


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
