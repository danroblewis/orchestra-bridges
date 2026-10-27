"""The RabbitMQ preset, loaded by Orchestra from a project's ``[[sources]]`` table and read from a
store holding RabbitMQ's real ``rabbitmq_detailed_*`` series: each series counts what it should
(published per exchange, acknowledged and waiting per queue), in the right direction, on the right
channel, for the right machine."""

import random
import subprocess
from pathlib import Path

import pytest
from fake_rabbit_store import FakeRabbitStore
from orchestra.config import load_project
from orchestra.metrics import for_project
from orchestra.sources.catalogue import Catalogue
from orchestra.sources.spec import for_project as specs_for

PRESET = Path(__file__).resolve().parents[2] / "orchestra_bridges" / "rabbitmq" / "preset.toml"
T0 = 1_800_000_000.0
SCRAPE = 15.0
STEPS = 320                               # 80 minutes of scrapes; the test reads the last hour
ROUTES = {"orders": ["billing.orders", "analytics.orders"], "clicks": ["analytics.clicks"]}
CHANNELS = {"<0.101.0>": "orders", "<0.102.0>": "orders", "<0.201.0>": "clicks", "<0.301.0>": "", "<0.302.0>": "amq.direct"}


def world(seed: int = 5):
    """RabbitMQ's detailed series every 15 s (7 s past each grid line), and the truth: each queue's
    messages at each scrape."""
    rng = random.Random(seed)
    def lab(**k):
        return {"vhost": "/", **k}

    pub = dict.fromkeys(CHANNELS, 0)
    routed = {q: 0 for qs in ROUTES.values() for q in qs}
    acked = dict.fromkeys(routed, 0)
    series: dict[tuple, list] = {}

    def put(name, labels, t, v):
        series.setdefault((name, tuple(sorted(labels.items()))), []).append((t, float(v)))

    truth = {}
    for k in range(STEPS):
        t = T0 + SCRAPE * k + 7
        for ch, ex in CHANNELS.items():
            n = rng.randint(0, 4)
            pub[ch] += n
            for q in ROUTES.get(ex, []):
                routed[q] += n
        for q, sent in routed.items():
            waiting = sent - acked[q]
            acked[q] += waiting if q.startswith("billing") and k % 4 == 3 else min(waiting, rng.randint(0, 3))
        for ch, ex in CHANNELS.items():
            put("rabbitmq_detailed_channel_messages_published_total", lab(channel=ch, exchange=ex), t, pub[ch])
        for ex, qs in ROUTES.items():
            for q in qs:
                put("rabbitmq_detailed_queue_exchange_messages_published_total", lab(queue=q, exchange=ex), t, routed[q])
        for q, sent in routed.items():
            put("rabbitmq_detailed_queue_messages_acked_total", lab(queue=q), t, acked[q])
            put("rabbitmq_detailed_queue_messages", lab(queue=q), t, sent - acked[q])
            put("rabbitmq_detailed_queue_consumers", lab(queue=q), t, 1)
            truth[(q, t)] = sent - acked[q]
    return [(name, dict(labels), pts) for (name, labels), pts in series.items()], truth


@pytest.fixture
def rabbit(tmp_path, monkeypatch):
    dump, truth = world()
    store = FakeRabbitStore(dump)
    monkeypatch.setenv("ORCHESTRATOR_HOME", str(tmp_path / "home"))
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    (repo / "orchestra.toml").write_text(f"""[discovery]
window = "60m"
step = "15s"

[[sources]]
name = "rabbit"
url = "{store.url}"
preset = "{PRESET}"

[sources.producers]
orders = "orders-api"
""")
    yield load_project(repo), dump, truth
    store.close()


def test_the_preset_loads_with_nothing_refused(rabbit):
    project, _, _ = rabbit
    specs = specs_for(project)
    assert not specs.refused, specs.refused
    assert [s.name for s in specs.sources] == ["rabbit"] and not specs.sources[0].infer
    assert {d.name for d in specs.declared} == {"rabbitmq_published_total", "rabbitmq_acked_total", "rabbitmq_backlog", "rabbitmq_consumers"}


def test_each_series_counts_its_direction_channel_and_machine(rabbit):
    project, dump, truth = rabbit
    start, end = T0 + SCRAPE * (STEPS - 240), T0 + SCRAPE * (STEPS - 1)
    reading = Catalogue(project, for_project(project)).read(start, end, SCRAPE)
    assert not reading.missing, reading.missing
    got = {(name, tuple(sorted((k, v) for k, v in labels.items() if k in ("exchange", "queue")))): pts for name, labels, pts in reading.dump}
    said = {(name, tuple(sorted((k, v) for k, v in labels.items() if k in ("exchange", "queue")))): reading.traffic.get(key)
            for name, labels, _ in reading.dump for key in [_key(name, labels)]}

    published = {k[1][0][1]: v for k, v in said.items() if k[0] == "rabbit:rabbitmq_published_total"}
    assert set(published) == {"orders", "clicks"}            # the default exchange and amq.* are not traffic
    assert published["orders"] == {"direction": "out", "channel": "rabbit:orders", "machine": "rabbit.orders-api",
                                   "attributed": "declared in [sources.producers]"}
    assert published["clicks"]["machine"] is None and published["clicks"]["direction"] == "out"
    def sent(ex):
        return sum(_last(pts, end) - _first(pts, start) for n, lab, pts in dump
                   if n == "rabbitmq_detailed_channel_messages_published_total" and lab["exchange"] == ex)

    for ex in ("orders", "clicks"):
        assert got[("rabbit:rabbitmq_published_total", (("exchange", ex),))][-1][1] == sent(ex)

    for q, ex, who in (("billing.orders", "orders", "billing"), ("analytics.orders", "orders", "analytics"),
                       ("analytics.clicks", "clicks", "analytics")):
        key = (("exchange", ex), ("queue", q))
        assert said[("rabbit:rabbitmq_acked_total", key)] == {"direction": "in", "channel": f"rabbit:{ex}", "machine": f"rabbit.{who}",
                                                              "attributed": "from its labels"}
        assert said[("rabbit:rabbitmq_backlog", key)]["direction"] == "backlog"
        assert said[("rabbit:rabbitmq_backlog", key)]["machine"] == f"rabbit.{who}"
        backlog = got[("rabbit:rabbitmq_backlog", key)]
        assert len(backlog) >= 239
        for t, v in backlog:                          # each step reads the scrape 8 s before it
            assert v == truth[(q, t - SCRAPE + 7)], (q, t)
    consumers = [labels for name, labels, _ in reading.dump if name == "rabbit:rabbitmq_consumers"]
    assert {lab["machine"] for lab in consumers} == {"rabbit.billing", "rabbit.analytics"}


def _key(name, labels):
    from orchestra.sd.series import series_key
    return series_key(name, labels)


def _first(pts, start):
    return next(v for t, v in pts if t >= start)


def _last(pts, end):
    return [v for t, v in pts if t <= end][-1]
