"""promql_example against the kit's Prometheus fake: ``measure`` prints each query's answer as gauges with
the aliases applied, and refuses an answer over ``max_series``; no token leaves.

Run with Orchestra's own Python (the one ``orchestra`` runs in, which has the kits): ``python -m pytest .orchestra/machines/promql_example/test_promql_example.py``."""

import importlib.util
from pathlib import Path

import pytest

from orchestra_bridges.kits.fakes import FakePrometheus
from orchestra_bridges.kits.testing import fake_token, set_args, token_env

HERE = Path(__file__).resolve().parent
TOKEN = fake_token(HERE)        # the placeholder the runner sends a fake on this machine
REAL = "real-token-never-sent-to-a-fake"   # in the environment, yet never read for a fake
TOKEN_ENV = token_env(HERE, "PROMETHEUS_TOKEN")      # machine.toml's, so the owner's variable works too
# The queries the fake's series answer (the owner's own queries are theirs to try against the store).
SCOPE = set_args(aliases={"k8s_cluster": "cluster"}, max_series=200, queries=[
    {"name": "http_requests", "query": 'sum by (service) (http_requests_total{k8s_cluster="prod-1"})',
     "help": "Requests served, by service."},
    {"name": "queue_depth", "query": "queue_depth", "help": "Messages waiting, by queue."}])


def program():
    spec = importlib.util.spec_from_file_location("promql_example", HERE / "promql_example.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def fake(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCHESTRATOR_HOME", str(tmp_path / "home"))
    monkeypatch.setenv(TOKEN_ENV, REAL)
    with FakePrometheus(token=TOKEN) as f:
        yield f


def measure(fake, tmp):
    return program().main(["measure", "--base-url", fake.url, "--state-dir", str(tmp / "state"), "--ttl", "0", *SCOPE])


def test_measure_prints_the_queries(fake, tmp_path, capsys):
    assert measure(fake, tmp_path) == 0
    out = capsys.readouterr().out
    assert 'http_requests{service="checkout"} 240' in out
    assert 'queue_depth{cluster="prod-2",queue="orders"} 5' in out          # k8s_cluster aliased
    assert all(r.authorized for r in fake.requests)


def test_too_many_series_fails(fake, tmp_path, capsys):
    for n in range(300):
        fake.add_sample({"__name__": "queue_depth", "queue": f"q{n}"}, 0, 1)
    assert measure(fake, tmp_path) == 1
    assert "max_series" in capsys.readouterr().err


def test_no_token_leaves(fake, tmp_path, capsys):
    measure(fake, tmp_path)
    out = capsys.readouterr()
    assert REAL not in out.out + out.err
    for p in tmp_path.rglob("*"):
        if p.is_file():
            assert REAL not in p.read_text(errors="replace"), p
