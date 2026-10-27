"""The RabbitMQ preset against the rig's real RabbitMQ and VictoriaMetrics (opt in:
``ORCHESTRA_RABBIT_RIG_URL=http://127.0.0.1:8441`` after ``orchestra_bridges/rabbitmq/rig/rig.sh up``).

The unit test's fake evaluates the preset's joins itself; this checks that a real store does the
same: every series the fleet makes is read, nothing is missing, and each counts its direction,
channel and machine. Read-only queries."""

import os
import subprocess
import time
from pathlib import Path

import pytest
from orchestra.config import load_project
from orchestra.metrics import for_project
from orchestra.sd.rediscover import aligned
from orchestra.sd.series import series_key
from orchestra.sources.catalogue import Catalogue

URL = os.environ.get("ORCHESTRA_RABBIT_RIG_URL")
pytestmark = pytest.mark.skipif(not URL, reason="set ORCHESTRA_RABBIT_RIG_URL to the rig's VictoriaMetrics (rig/rig.sh up)")
PRESET = Path(__file__).resolve().parents[2] / "orchestra_bridges" / "rabbitmq" / "preset.toml"
CONSUMED = {"billing.orders": ("orders", "billing"), "billing.payments": ("payments", "billing"), "shipping.orders": ("orders", "shipping"),
            "notifier.payments": ("payments", "notifier"), "analytics.orders": ("orders", "analytics"),
            "analytics.clicks": ("clicks", "analytics")}


def test_the_rigs_fleet_is_read_as_counted_traffic(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCHESTRATOR_HOME", str(tmp_path / "home"))
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    (repo / "orchestra.toml").write_text(f"""[[sources]]
name = "rabbit"
url = "{URL}"
preset = "{PRESET}"
settle = "30s"

[sources.producers]
orders = "orders-api"
""")
    project = load_project(repo)
    now = time.time()
    start, end = aligned(now - 1200, now, 15.0)
    reading = Catalogue(project, for_project(project)).read(start, end, 15.0)
    assert not reading.missing, reading.missing
    said = {}
    for name, labels, pts in reading.dump:
        said[(name, labels.get("exchange"), labels.get("queue"))] = (reading.traffic.get(series_key(name, labels)), pts)
    for ex, who in (("orders", "rabbit.orders-api"), ("payments", None), ("clicks", None)):
        t, pts = said[("rabbit:rabbitmq_published_total", ex, None)]
        assert t["direction"] == "out" and t["channel"] == f"rabbit:{ex}" and t["machine"] == who
        assert pts[-1][1] > 0, f"nothing published into {ex} in 20 minutes: is the fleet running?"
    for q, (ex, consumer) in CONSUMED.items():
        for name, direction in (("rabbit:rabbitmq_acked_total", "in"), ("rabbit:rabbitmq_backlog", "backlog")):
            t, _ = said[(name, ex, q)]
            assert (t["direction"], t["channel"], t["machine"]) == (direction, f"rabbit:{ex}", f"rabbit.{consumer}"), (name, q, t)
