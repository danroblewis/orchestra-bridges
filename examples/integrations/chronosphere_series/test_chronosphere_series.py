"""chronosphere_series against the kit's Prometheus fake. The preferred strategy: ``check_source.py``
finds the ``[[sources]]`` entry of ``sources.toml`` accepted, the store answering and the aliases
applied, refuses an uncleared store before any read, and fails when an alias does not apply. The
fallback: ``measure`` prints each query's answer as gauges with the aliases applied (twice: two
``[[collect]]`` rounds), and refuses an answer over ``max_series``; no token leaves.

Run with Orchestra's own Python (the one ``orchestra`` runs in, which has the kits): ``python -m pytest examples/integrations/chronosphere_series/test_chronosphere_series.py``."""

import importlib.util
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from orchestra_bridges.kits.fakes import FakePrometheus
from orchestra_bridges.kits.testing import fake_token

HERE = Path(__file__).resolve().parent
TOKEN = fake_token(HERE)        # the placeholder the runner sends a fake on this machine
REAL = "real-token-never-sent-to-a-fake"   # in the environment, yet never read for a fake


def load(name):
    spec = importlib.util.spec_from_file_location(name, HERE / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def program():
    spec = importlib.util.spec_from_file_location("chronosphere_series", HERE / "chronosphere_series.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def fake(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCHESTRATOR_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("CHRONOSPHERE_TOKEN", TOKEN)
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
    assert REAL not in out.out + out.err
    for p in tmp_path.rglob("*"):
        if p.is_file():
            assert REAL not in p.read_text(errors="replace"), p


def test_two_rounds_print_the_series_again_from_a_fake_with_no_token(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ORCHESTRATOR_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("CHRONOSPHERE_TOKEN", raising=False)
    with FakePrometheus() as f:                          # the fake needs no token
        assert measure(f, tmp_path) == 0
        f.add_sample({"__name__": "queue_depth", "queue": "orders", "k8s_cluster": "prod-2"}, 1, 8)
        time.sleep(1.1)
        assert measure(f, tmp_path) == 0
    out = capsys.readouterr().out
    assert 'queue_depth{cluster="prod-2",queue="orders"} 5' in out and 'queue_depth{cluster="prod-2",queue="orders"} 8' in out


def test_the_fallback_reads_only_a_fake_until_cleared(tmp_path, monkeypatch):
    monkeypatch.setenv("CHRONOSPHERE_TOKEN", TOKEN)
    assert program().main(["measure", "--state-dir", str(tmp_path / "s")]) == 2      # poller.toml's real URL


def test_the_sources_entry_is_accepted_answers_and_its_aliases_apply(fake, tmp_path, capsys):
    assert load("check_source").main(["--url", fake.url]) == 0
    out = capsys.readouterr().out
    assert "answers" in out and "ok: the store answers and the aliases apply" in out
    assert "chrono:queue_depth {cluster=prod-2, queue=orders}" in out                  # k8s_cluster -> cluster
    assert "k8s_cluster=" not in out and REAL not in out
    assert "CHRONOSPHERE_TOKEN: present" in out                                       # named, never shown
    assert fake.requests and all(r.authorized for r in fake.requests)


def test_the_sources_check_needs_no_token_for_a_fake(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("ORCHESTRATOR_HOME", str(tmp_path / "home"))
    monkeypatch.delenv("CHRONOSPHERE_TOKEN", raising=False)
    with FakePrometheus() as f:
        assert load("check_source").main(["--url", f.url]) == 0
    assert "auth none" in capsys.readouterr().out


def test_the_alias_check_names_a_spelling_left_unrenamed():
    mod = load("check_source")
    spec = SimpleNamespace(labels={"cluster": ["k8s_cluster", "cluster"]})
    assert mod.aliases_apply(spec, [("chrono:q", {"k8s_cluster": "a"})])            # a renamed spelling still there
    assert not mod.aliases_apply(spec, [("chrono:q", {"cluster": "a"})])


def test_the_sources_check_reads_only_a_fake_until_cleared(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CHRONOSPHERE_TOKEN", TOKEN)
    assert load("check_source").main([]) == 2                                         # sources.toml's real URL
    assert "not cleared" in capsys.readouterr().err
