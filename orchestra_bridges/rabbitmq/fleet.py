"""A simulated fleet of services talking over RabbitMQ, for testing counted traffic: three publish,
four consume, and one consumer is too slow at peak.

    uv run --no-project --with pika python orchestra_bridges/rabbitmq/fleet.py --port 5682

It is a test system, not a bridge: it never touches Orchestra. Orchestra sees it only through
RabbitMQ's own per-queue counts (``preset.toml``), never a message.

| Service | Does | Exchange (fanout) → queues |
|---|---|---|
| ``orders-api`` | publishes about 4 a second | ``orders`` → ``billing.orders``, ``shipping.orders``, ``analytics.orders`` |
| ``payments-api`` | publishes about 2 a second | ``payments`` → ``billing.payments``, ``notifier.payments`` |
| ``web`` | publishes about 6 a second | ``clicks`` → ``analytics.clicks`` |
| ``billing`` | takes everything, acknowledges it in a batch every 20 s | ``billing.orders``, ``billing.payments`` |
| ``shipping`` | 50 ms a message, keeps up | ``shipping.orders`` |
| ``notifier`` | 20 ms a message, keeps up | ``notifier.payments`` |
| ``analytics`` | 70 ms a message (about 14 a second): its backlog grows at peak and drains after | ``analytics.orders``, ``analytics.clicks`` |

Demand follows a day compressed into ``--period`` (20 minutes): each publisher's rate is its mean
times 1 + 0.7 sin(2π t / period), so at peak the orders and clicks reaching analytics (about 17 a
second) outrun it, and in the trough it catches up. Queues are named ``<consumer>.<what>``, the
naming the preset reads consumers from. Each service is its own process with its own connection,
named after it. SIGTERM stops them all."""

from __future__ import annotations

import argparse
import math
import multiprocessing as mp
import os
import random
import signal
import sys
import time

import pika

EXCHANGES = {"orders": ["billing.orders", "shipping.orders", "analytics.orders"],
             "payments": ["billing.payments", "notifier.payments"],
             "clicks": ["analytics.clicks"]}
PUBLISHERS = {"orders-api": ("orders", 4.0), "payments-api": ("payments", 2.0), "web": ("clicks", 6.0)}
CONSUMERS = {"billing": (["billing.orders", "billing.payments"], "batch", 20.0),
             "shipping": (["shipping.orders"], "steady", 0.05),
             "notifier": (["notifier.payments"], "steady", 0.02),
             "analytics": (["analytics.orders", "analytics.clicks"], "steady", 0.07)}
SWING = 0.7


def connect(args, name: str) -> pika.BlockingConnection:
    params = pika.ConnectionParameters(args.host, args.port, heartbeat=30, client_properties={"connection_name": name})
    while True:
        try:
            return pika.BlockingConnection(params)
        except pika.exceptions.AMQPConnectionError:
            time.sleep(2)


def topology(ch) -> None:
    for ex, queues in EXCHANGES.items():
        ch.exchange_declare(ex, "fanout", durable=True)
        for q in queues:
            ch.queue_declare(q, durable=True)
            ch.queue_bind(q, ex)


def demand(mean: float, period: float, t0: float) -> float:
    return mean * (1 + SWING * math.sin(2 * math.pi * ((time.time() - t0) % period) / period))


def publisher(args, name: str) -> None:
    exchange, mean = PUBLISHERS[name]
    conn = connect(args, name)
    ch = conn.channel()
    topology(ch)
    rng = random.Random(name)
    while True:
        ch.basic_publish(exchange, "", b"{}", pika.BasicProperties(app_id=name))
        conn.sleep(rng.expovariate(max(0.05, demand(mean, args.period, args.t0))))


def consumer(args, name: str) -> None:
    queues, how, every = CONSUMERS[name]
    conn = connect(args, name)
    ch = conn.channel()
    topology(ch)
    if how == "batch":            # take what comes, acknowledge it all every `every` seconds
        ch.basic_qos(prefetch_count=2000)
        last = {"tag": None}

        def take(c, method, props, body):
            last["tag"] = method.delivery_tag
        for q in queues:
            ch.basic_consume(q, take)
        while True:
            until = time.monotonic() + every         # process_data_events returns once it has dispatched something
            while (left := until - time.monotonic()) > 0:
                conn.process_data_events(time_limit=left)
            if last["tag"] is not None:
                ch.basic_ack(last["tag"], multiple=True)
                last["tag"] = None
    else:                         # one at a time, `every` seconds each
        ch.basic_qos(prefetch_count=10)

        def work(c, method, props, body):
            time.sleep(every)
            c.basic_ack(method.delivery_tag)
        for q in queues:
            ch.basic_consume(q, work)
        ch.start_consuming()


def run(args, name: str) -> None:
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
    while True:                   # a dropped connection is reopened, as a service would
        try:
            (publisher if name in PUBLISHERS else consumer)(args, name)
        except (pika.exceptions.AMQPError, OSError) as e:
            print(f"{name}: {type(e).__name__}: {e}; reconnecting", flush=True)
            time.sleep(2)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=5682)
    ap.add_argument("--period", type=float, default=1200.0, help="seconds in one simulated day (default 20 minutes)")
    ap.add_argument("--pidfile")
    args = ap.parse_args()
    args.t0 = time.time()
    if args.pidfile:
        with open(args.pidfile, "w") as fh:
            fh.write(str(os.getpid()))
    procs = [mp.Process(target=run, args=(args, n), name=n, daemon=True) for n in [*CONSUMERS, *PUBLISHERS]]
    for p in procs:
        p.start()
        time.sleep(0.3)           # consumers first, so no queue is declared after its publisher starts

    def stop(*_):
        for p in procs:
            p.terminate()
        sys.exit(0)
    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    print(f"fleet: {len(procs)} services on {args.host}:{args.port}, a day every {args.period:g} s", flush=True)
    while True:
        for p in procs:
            if not p.is_alive():
                print(f"fleet: {p.name} exited ({p.exitcode})", flush=True)
                stop()
        time.sleep(5)


if __name__ == "__main__":
    sys.exit(main())
