"""Model acceptance for the integration examples (examples/integrations/): their series, over a
simulated hour and a half with the matching counts of the bus (``integrations_sim``), fed into
Orchestra's own discovery against a file metrics store, as one system. Discovery must find exactly
the stocks the worlds have, joined by the pollers' counters, and never make an age or a wandering
queue that nothing counts a stock (the forbidden list)."""

from types import SimpleNamespace

import pytest

pytest.importorskip("numpy")

from integrations_sim import simulate
from orchestra.metrics.file import FileMetrics
from orchestra.sd.rediscover import rediscover
from orchestra.sd.strategies import CONSERVATION

EXPECTED = {
    ("pagerduty_incidents_open_total", (("urgency", "high"),)),
    ("pagerduty_incidents_open_total", (("urgency", "low"),)),
    ("jira_issues_total", (("status", "Open"),)),
    ("jira_issues_total", (("status", "In Progress"),)),
    ("jira_issues_total", (("status", "In Review"),)),
    ("slack_mentions_open_total", ()),
}
FORBIDDEN = {"jira_issue_age_seconds", "queue_depth", "slack_messages_total", "orchestra_events_total"}
WHO = {"project", "instrument"}


def what(ref) -> tuple[str, tuple]:
    return ref.name, tuple(sorted((k, v) for k, v in ref.labels.items() if k not in WHO))


@pytest.fixture(scope="module", params=[7, 23])
def model(request, tmp_path_factory):
    tmp = tmp_path_factory.mktemp(f"sim{request.param}")
    store = FileMetrics(tmp / "metrics.jsonl")
    end = simulate(store, seed=request.param)
    conf = {"discovery_strategy": CONSERVATION, "discovery_options": {}, "discovery_window": 3600.0,
            "discovery_step": 30.0, "discovery_every": 300.0, "eras_from": [], "prior_for": None}
    project = SimpleNamespace(key="sim", state_dir=tmp / "state", repo_root=tmp, config=SimpleNamespace(**conf))
    return rediscover(project, store, now=end).model


def test_the_stocks_are_the_open_incidents_the_issues_by_status_and_the_open_mentions(model):
    assert {what(s.series) for s in model.stocks} == EXPECTED


def test_an_age_and_an_uncounted_queue_never_become_stocks(model):
    names = {s.series.name for s in model.stocks}
    assert not names & FORBIDDEN
    left = model.discovery["left_out"]
    for status in ("Open", "In Progress", "In Review"):
        key = f'jira_issue_age_seconds{{instrument="jira_poller",status="{status}"}}'
        assert "not a stock" in left[key], left.get(key)
    assert any(k.startswith("queue_depth{") for k in left)


def test_the_pollers_counters_are_the_flows_and_join_the_statuses(model):
    stock_of = {s.id: what(s.series) for s in model.stocks}
    ends = {(f.counter.name, tuple(sorted((k, v) for k, v in f.counter.labels.items() if k not in WHO))):
            (stock_of.get(f.src), stock_of.get(f.dst)) for f in model.flows if f.counter}
    pd_high = ("pagerduty_incidents_open_total", (("urgency", "high"),))
    assert ends[("pagerduty_incidents_triggered_total", (("urgency", "high"),))] == (None, pd_high)
    assert ends[("pagerduty_incidents_resolved_total", (("urgency", "high"),))] == (pd_high, None)
    status = {s: ("jira_issues_total", (("status", s),)) for s in ("Open", "In Progress", "In Review")}
    assert ends[("jira_issues_created_total", (("status", "Open"),))] == (None, status["Open"])
    assert ends[("jira_issue_transitions_total", (("from_status", "Open"), ("to_status", "In Progress")))] == (
        status["Open"], status["In Progress"])                    # one flow from the status left to the one entered
    assert ends[("jira_issue_transitions_total", (("from_status", "In Progress"), ("to_status", "In Review")))] == (
        status["In Progress"], status["In Review"])
    mentions = ("slack_mentions_open_total", ())
    assert ends[("slack_mentions_total", ())] == (None, mentions)
    assert ends[("slack_mentions_answered_total", ())] == (mentions, None)
    for f in model.flows:
        assert f.current() is not None and f.current().origin == "fitted"
