#!/usr/bin/env bash
# The RabbitMQ counted-traffic rig: a RabbitMQ with its Prometheus plugin, a VictoriaMetrics that
# scrapes it every 5 s, and the simulated fleet (fleet.py) publishing and consuming through it.
#
#   rig.sh up       start all three (idempotent); the fleet's log and pid are in $RIG_STATE
#   rig.sh down     stop the fleet and remove both containers
#   rig.sh status   what is running
#
# Ports (127.0.0.1 only): AMQP 5682, management 15682, RabbitMQ's metrics 15702, VictoriaMetrics 8441.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
RIG_STATE="${RIG_STATE:-${XDG_STATE_HOME:-$HOME/.local/state}/orchestra-rabbit-rig}"
NET=orchestra-test-rabbit-net
mkdir -p "$RIG_STATE"

up() {
  docker network inspect "$NET" >/dev/null 2>&1 || docker network create "$NET" >/dev/null
  if ! docker inspect orchestra-test-rabbit >/dev/null 2>&1; then
    docker run -d --name orchestra-test-rabbit --network "$NET" --hostname orchestra-test-rabbit \
      -p 127.0.0.1:5682:5672 -p 127.0.0.1:15682:15672 -p 127.0.0.1:15702:15692 \
      -v "$HERE/enabled_plugins:/etc/rabbitmq/enabled_plugins:ro" \
      -v "$HERE/rabbitmq.conf:/etc/rabbitmq/conf.d/90-rig.conf:ro" rabbitmq:4.0.3-management >/dev/null
  fi
  if ! docker inspect orchestra-test-rabbit-vm >/dev/null 2>&1; then
    docker run -d --name orchestra-test-rabbit-vm --network "$NET" -p 127.0.0.1:8441:8428 \
      -v "$HERE/scrape.yml:/scrape.yml:ro" victoriametrics/victoria-metrics:v1.106.1 \
      -promscrape.config=/scrape.yml -retentionPeriod=7d >/dev/null
  fi
  for _ in $(seq 60); do curl -sf -o /dev/null http://127.0.0.1:15702/metrics && break; sleep 1; done
  if [ -f "$RIG_STATE/fleet.pid" ] && kill -0 "$(cat "$RIG_STATE/fleet.pid")" 2>/dev/null; then
    echo "fleet already running (pid $(cat "$RIG_STATE/fleet.pid"))"
  else
    nohup uv run --no-project --with pika python "$HERE/../fleet.py" --port 5682 --pidfile "$RIG_STATE/fleet.pid" \
      >"$RIG_STATE/fleet.log" 2>&1 &
    sleep 3
    echo "fleet started (pid $(cat "$RIG_STATE/fleet.pid"), log $RIG_STATE/fleet.log)"
  fi
}

down() {
  if [ -f "$RIG_STATE/fleet.pid" ]; then kill "$(cat "$RIG_STATE/fleet.pid")" 2>/dev/null || true; rm -f "$RIG_STATE/fleet.pid"; fi
  docker rm -f orchestra-test-rabbit orchestra-test-rabbit-vm >/dev/null 2>&1 || true
  docker network rm "$NET" >/dev/null 2>&1 || true
}

status() {
  docker ps --filter name=orchestra-test-rabbit --format '{{.Names}}\t{{.Status}}\t{{.Ports}}'
  if [ -f "$RIG_STATE/fleet.pid" ] && kill -0 "$(cat "$RIG_STATE/fleet.pid")" 2>/dev/null; then
    echo "fleet running (pid $(cat "$RIG_STATE/fleet.pid"))"
  else
    echo "fleet not running"
  fi
}

"${1:-status}"
