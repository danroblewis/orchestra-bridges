"""A simulated hour and a half of the three pollers' worlds (Jira, Slack, PagerDuty) and of the
Chronosphere fallback's gauges, as Orchestra would store them: each example's series as its
``[[collect]]`` entry writes them (a point a minute, labelled ``instrument=<name>``), and
Orchestra's count of the bus, ``orchestra_events_total{type, source}``, written when it moves, for
the events the pollers relay as the actors. The series names come from the examples' own modules.

The worlds are birth-death processes with known structure, so discovery's answer can be judged:
the open incidents of each urgency, the issues in each status and the open mentions are stocks
(the pollers' counters are their flows); an age and a queue that nothing counts are not."""

from __future__ import annotations

import importlib.util
import math
import random
from collections import defaultdict
from pathlib import Path

from orchestra.metrics import Point

EXAMPLES = Path(__file__).resolve().parents[1] / "examples" / "integrations"
T0 = 1_800_000_000.0
MINUTES = 90
TICK = 5.0                       # the worlds move every 5 s; the instruments read every 60 s
PROJECT = "sim"


def example(name: str):
    spec = importlib.util.spec_from_file_location(f"sim_{name}", EXAMPLES / name / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


PD, JP, SW = example("pagerduty_alerts"), example("jira_poller"), example("slack_watcher")


def poisson(rng: random.Random, mean: float) -> int:
    limit, k, p = math.exp(-mean), 0, 1.0
    while True:
        p *= rng.random()
        if p <= limit:
            return k
        k += 1


class Bus:
    """``orchestra_events_total{type, source}`` as the host's collector writes it: once per event."""

    def __init__(self):
        self.counts: dict[tuple[str, str], int] = defaultdict(int)
        self.points: list[Point] = []

    def event(self, type: str, source: str, t: float) -> None:
        self.counts[(type, source)] += 1
        self.points.append(Point("orchestra_events_total", self.counts[(type, source)], t,
                                 {"project": PROJECT, "type": type, "source": source}))


def collected(instrument: str, name: str, value: float, t: float, labels: dict | None = None) -> Point:
    return Point(name, value, t, {"project": PROJECT, "instrument": instrument, **(labels or {})})


def pick(rng, people):
    return people[rng.randrange(len(people))]


def pagerduty(rng, bus: Bus, t: float, st: dict, out: list[Point], minute: bool) -> None:
    pd = PD
    incidents, c = st.setdefault("open", []), st.setdefault("c", defaultdict(int))
    for urgency, rate in (("high", 0.25), ("low", 0.4)):
        for _ in range(poisson(rng, rate * TICK / 60)):
            incidents.append({"urgency": urgency, "acked": False})
            c[("triggered", urgency)] += 1
            bus.event("pagerduty.incident.triggered", "bot.pagerduty", t)
    for i in list(incidents):
        if not i["acked"] and rng.random() < 0.3 * TICK / 60:
            i["acked"] = True
            bus.event("pagerduty.incident.acknowledged", pick(rng, ["person.pagerduty.p1", "person.pagerduty.p2"]), t)
        elif rng.random() < 0.12 * TICK / 60:
            incidents.remove(i)
            c[("resolved", i["urgency"])] += 1
            bus.event("pagerduty.incident.resolved", pick(rng, ["person.pagerduty.p1", "person.pagerduty.p2",
                                                                "bot.pagerduty"]), t)
    if minute:
        for urgency in pd.URGENCIES:
            out.append(collected("pagerduty_alerts", "pagerduty_incidents_open_total",
                                 sum(1 for i in incidents if i["urgency"] == urgency), t, {"urgency": urgency}))
            for what in ("triggered", "resolved"):
                out.append(collected("pagerduty_alerts", f"pagerduty_incidents_{what}_total", c[(what, urgency)], t,
                                     {"urgency": urgency}))


FLOW = {"Open": ("In Progress", 0.08), "In Progress": ("In Review", 0.1), "In Review": ("Done", 0.15)}
STATUSES = ("Open", "In Progress", "In Review", "Done")


def jira(rng, bus: Bus, t: float, st: dict, out: list[Point], minute: bool) -> None:
    jp = JP
    issues, c = st.setdefault("issues", []), st.setdefault("c", defaultdict(int))
    people = ["person.jira.alice", "person.jira.bob", "person.jira.carol"]
    for _ in range(poisson(rng, 0.5 * TICK / 60)):
        issues.append({"status": "Open", "created": t})
        c[(jp.CREATED, "Open")] += 1
        bus.event("jira.issue.created", pick(rng, people), t)
    for i in issues:
        if i["status"] in FLOW and rng.random() < FLOW[i["status"]][1] * TICK / 60:
            to = FLOW[i["status"]][0]
            c[(jp.MOVED, i["status"], to)] += 1
            i["status"] = to
            bus.event("jira.issue.transitioned", pick(rng, people), t)
        elif rng.random() < 0.02 * TICK / 60:
            bus.event("jira.issue.updated", pick(rng, people), t)
    if minute:
        out.append(collected("jira_poller", jp.CREATED, c[(jp.CREATED, "Open")], t, {"status": "Open"}))
        for s in STATUSES:
            out.append(collected("jira_poller", "jira_issues_total", sum(1 for i in issues if i["status"] == s), t,
                                 {"status": s}))
            if s in FLOW:
                to = FLOW[s][0]
                out.append(collected("jira_poller", jp.MOVED, c[(jp.MOVED, s, to)], t, {"from_status": s, "to_status": to}))
            ages = [t - i["created"] for i in issues if i["status"] == s]
            if s != "Done" and ages:
                out.append(collected("jira_poller", "jira_issue_age_seconds", round(max(ages)), t, {"status": s}))


def slack(rng, bus: Bus, t: float, st: dict, out: list[Point], minute: bool) -> None:
    sw = SW
    c = st.setdefault("c", defaultdict(int))
    st.setdefault("open", 0)
    people = ["person.slack.u001", "person.slack.u002", "person.slack.u003"]
    for _ in range(poisson(rng, 2.0 * TICK / 60)):
        c[(sw.MESSAGES, "C001")] += 1
        bus.event("slack.message.posted", pick(rng, people), t)
    for _ in range(poisson(rng, 0.4 * TICK / 60)):
        st["open"] += 1
        c[sw.OPENED] += 1
        c[(sw.MESSAGES, "C001")] += 1
        bus.event("slack.message.posted", pick(rng, people[1:]), t)
    for _ in range(st["open"]):
        if rng.random() < 0.15 * TICK / 60:
            st["open"] -= 1
            c[sw.ANSWERED] += 1
            c[(sw.MESSAGES, "C001")] += 1
            bus.event("slack.message.posted", "person.slack.u001", t)
    if minute:
        out.append(collected("slack_watcher", sw.MESSAGES, c[(sw.MESSAGES, "C001")], t, {"channel": "C001"}))
        out.append(collected("slack_watcher", sw.OPENED, c[sw.OPENED], t))
        out.append(collected("slack_watcher", sw.ANSWERED, c[sw.ANSWERED], t))
        out.append(collected("slack_watcher", "slack_mentions_open_total", st["open"], t))


def chronosphere(rng, bus: Bus, t: float, st: dict, out: list[Point], minute: bool) -> None:
    """The fallback collector's gauge: a queue that wanders, no counter beside it."""
    st["q"] = max(0, st.get("q", 5) + rng.choice((-1, 0, 0, 1)))
    if minute:
        out.append(collected("chronosphere_series", "queue_depth", st["q"], t, {"queue": "orders", "cluster": "prod-2"}))


WORLDS = (pagerduty, jira, slack, chronosphere)


def simulate(store, seed: int = 7) -> float:
    """Write the worlds' series into ``store``; the time the simulation ends."""
    rng, bus = random.Random(seed), Bus()
    states = {w: {} for w in WORLDS}
    out: list[Point] = []
    n = int(MINUTES * 60 / TICK)
    for k in range(n + 1):
        t = T0 + k * TICK
        minute = k % int(60 / TICK) == 0
        for w in WORLDS:
            w(rng, bus, t, states[w], out, minute)
    store.write(out + bus.points)
    return T0 + n * TICK
