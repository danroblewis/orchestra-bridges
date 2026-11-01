# The RabbitMQ rig

A test system for Orchestra's counted traffic (`[[sources]]` over another system's bus), kept here
while the owner decides where it belongs (open question 7). It is not part of the plugin: nothing
here is installed or declared by it.

- `preset.toml`: series definitions over RabbitMQ's own per-queue counts (the `rabbitmq_prometheus`
  plugin's `/metrics/detailed`), for a project's `[[sources]]` (`preset = "<this path>"`).
- `fleet.py`: a simulated fleet of services talking over RabbitMQ.
- `rig.sh up|down|status`: a RabbitMQ and a VictoriaMetrics that scrapes it (`scrape.yml`,
  `rabbitmq.conf`, `enabled_plugins`); `orchestra.toml`: a project reading it through the preset.

Tests: `tests/unit/test_rabbitmq_preset.py` (against a fake store), `tests/contract/test_rabbitmq_live.py`
(against the rig, opt in with `ORCHESTRA_RABBIT_RIG_URL`).
