"""{{name}} against the kit's Prometheus fake: ``measure`` prints each query's answer as gauges with
the aliases applied, and refuses an answer over ``max_series``; no token leaves.

Run: ``uv run python -m pytest {{dir}}/test_{{name}}.py`` (in an environment with orchestra-bridges)."""

import importlib.util
from pathlib import Path

import pytest

from orchestra_bridges.kits.fakes import FakePrometheus

HERE = Path(__file__).resolve().parent
TOKEN = "test-token-not-real"


def program():
    spec = importlib.util.spec_from_file_location("{{name}}", HERE / "{{name}}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def fake(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCHESTRATOR_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("PROMETHEUS_TOKEN", TOKEN)
    with FakePrometheus(token=TOKEN) as f:
        yield f


def measure(fake, tmp):
    return program().main(["measure", "--base-url", fake.url, "--state-dir", str(tmp / "state"), "--ttl", "0"])


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
    assert TOKEN not in out.out + out.err
    for p in tmp_path.rglob("*"):
        if p.is_file():
            assert TOKEN not in p.read_text(errors="replace"), p
