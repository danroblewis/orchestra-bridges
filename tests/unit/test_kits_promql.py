"""The kits' Prometheus helper against the kit's Prometheus fake: instant, range and series
queries as plain series, label aliases, the max_series guard, and a bad query's error."""

import pytest

from orchestra_bridges.kits import CachedClient, PromError, Prometheus, TooManySeries
from orchestra_bridges.kits.fakes import FakePrometheus

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
