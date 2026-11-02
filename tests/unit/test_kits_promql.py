"""The kits' Prometheus helper against the kit's Prometheus fake: instant, range and series
queries as plain series, label aliases, the max_series guard, and a bad query's error."""

import pytest

from orchestra_bridges.kits import CachedClient, PromError, Prometheus, TooManySeries, Unbounded
from orchestra_bridges.kits.fakes import FakePrometheus
from orchestra_bridges.kits.promql import bounded

T0 = 1_793_000_000.0


@pytest.fixture
def prom(tmp_path):
    with FakePrometheus(t0=T0, token="t") as fake:
        client = CachedClient(fake.url, cache_dir=tmp_path / "c", rate_dir=tmp_path / "r", per_minute=6000,
                              headers={"Authorization": "Bearer t"})
        yield fake, Prometheus(client, aliases={"k8s_cluster": "cluster"}, max_series=10)


def test_instant_as_plain_series_with_aliases(prom):
    fake, p = prom
    out = p.instant('http_requests_total{service="checkout"}', at=T0)
    assert len(out) == 1
    s = out[0]
    assert s.name == "http_requests_total" and s.labels == {"service": "checkout", "cluster": "prod-1"}
    assert s.points == [(T0, 240.0)] and s.last == 240.0
    assert fake.requests[-1].method == "POST"


def test_instant_sum_by(prom):
    _, p = prom
    out = {s.labels["service"]: s.last for s in p.instant("sum by (service) (http_requests_total)", at=T0 - 300)}
    assert out == {"checkout": 160.0, "payments": 55.0}


def test_range(prom):
    _, p = prom
    (s,) = p.range("queue_depth", T0 - 600, T0, 300)
    assert [v for _, v in s.points] == [3.0, 9.0, 5.0]
    assert s.labels == {"queue": "orders", "cluster": "prod-2"}


def test_series(prom):
    _, p = prom
    rows = p.series('{__name__=~"http_.*"}')
    assert {r["service"] for r in rows} == {"checkout", "payments"} and all("cluster" in r for r in rows)


def test_max_series_guard(prom):
    fake, p = prom
    for n in range(20):
        fake.add_sample({"__name__": "many", "n": str(n)}, 0, 1)
    with pytest.raises(TooManySeries):
        p.instant("many", at=T0)


def test_bad_query_is_an_error(prom, tmp_path):
    fake, _ = prom
    client = CachedClient(fake.url, cache_dir=tmp_path / "c2", rate_dir=tmp_path / "r", per_minute=6000,
                          headers={"Authorization": "Bearer t"}, retries=0)
    with pytest.raises((PromError, Exception)) as e:
        Prometheus(client).instant("rate(x[5m]")
    assert "400" in str(e.value) or "parse" in str(e.value)


def test_answers_are_cached(prom):
    fake, p = prom
    p.instant("queue_depth", at=T0)
    p.instant("queue_depth", at=T0)
    assert len(fake.requests_to("/api/v1/query")) == 1


def test_a_series_lookup_is_bounded(prom):
    """Never the whole store: a selector must name something, the window is the last hour, and the
    store is asked for at most max_series + 1 (Prometheus's ``limit``)."""
    fake, p = prom
    for sel in ("{}", '{__name__=~".+"}', '{__name__=~".*"}', '{job!="x"}', ""):
        assert not bounded(sel)
        with pytest.raises(Unbounded):
            p.series(sel)
    assert not fake.requests                                   # refused before any request
    assert bounded("up") and bounded('{namespace="team-a"}') and bounded('{job=~"team-.*"}')
    p.series('{k8s_cluster="prod-1"}')
    (req,) = fake.requests_to("/api/v1/series")
    assert req.query["limit"] == ["11"] and float(req.query["end"][0]) - float(req.query["start"][0]) == 3600


def test_a_store_that_ignores_the_limit_is_still_refused(prom):
    fake, p = prom
    for n in range(20):
        fake.add_sample({"__name__": "many", "team": "a", "n": str(n)}, 0, 1)
    with pytest.raises(TooManySeries):
        p.series('{team="a"}')                  # the fake truncates to 11 with a warning: more than 10


def test_schema_is_names_and_label_names(prom):
    _, p = prom
    assert p.schema(['{k8s_cluster="prod-1"}', '{k8s_cluster="prod-2"}']) == {
        "http_requests_total": ["cluster", "service"], "queue_depth": ["cluster", "queue"]}
