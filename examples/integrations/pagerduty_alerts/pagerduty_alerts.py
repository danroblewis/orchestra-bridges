"""pagerduty_alerts: a poller that relays PagerDuty's incident changes (built with ``orchestra
bridges scaffold pagerduty pagerduty_alerts``, then completed; AGENTS.md says what it may do).

``python pagerduty_alerts.py`` (the poller): each poll lists the open incidents (triggered and
acknowledged) and reads by id each incident that was open last poll and is not now (resolved,
usually). It publishes ``pagerduty.incident.<status>`` once per change, as whoever changed it
(``person.pagerduty.<user id>``, or ``bot.pagerduty`` when the service did), with the incident's id
as the subject and its ``last_status_change_at`` as the stamp.

It also counts, by urgency, the incidents it first sees open
(``pagerduty_incidents_triggered_total{urgency}``) and those that stopped being open
(``pagerduty_incidents_resolved_total{urgency}``): Orchestra counts the events by type and source
only (it never reads ``data``), so these are what balances the open incidents of each urgency.
They are kept in the poller's ``cursor.json`` with the cursor: a restart neither loses nor repeats
a count.

``python pagerduty_alerts.py measure`` (for a ``[[collect]]`` entry): prints
``pagerduty_incidents_open{urgency}`` (a gauge: the open incidents, every urgency printed,
0 included) and the poller's two counters.

It keeps metadata only: ids, numbers, states, times, the service's and the user's ids. Never a
title, summary or name."""

from __future__ import annotations

import sys
from pathlib import Path

from orchestra_bridges.kits import runner
from orchestra_bridges.kits.poller import actor_source
from orchestra_bridges.kits.pollstate import counters_at

HERE = Path(__file__).resolve().parent
PAGE = 100
HEADERS = {"Accept": "application/vnd.pagerduty+json;version=2"}
URGENCIES = ("high", "low")


def meta(incident: dict) -> dict:
    """What is kept of an incident: never its title, summary or a name."""
    by = incident.get("last_status_change_by") or {}
    return {"id": incident["id"], "number": incident.get("incident_number"), "status": incident["status"],
            "urgency": incident.get("urgency"), "service": (incident.get("service") or {}).get("id"),
            "changed_at": incident.get("last_status_change_at"),
            "by": by.get("id") if by.get("type") == "user_reference" else None}


def keep_list(body: dict) -> dict:
    return {"incidents": [meta(i) for i in body.get("incidents") or []], "more": bool(body.get("more"))}


def keep_one(body: dict) -> dict:
    return meta(body["incident"])


def open_incidents(api, settings) -> list[dict]:
    out, offset = [], 0
    statuses = settings.extra.get("open_statuses", ["triggered", "acknowledged"])
    while True:
        page = api.get("/incidents", {"statuses[]": statuses, "offset": offset, "limit": PAGE,
                                      "sort_by": "created_at"}, keep=keep_list)
        out += page["incidents"]
        if not page["more"]:
            return out
        offset += PAGE


def make_poll(api, settings):
    def poll(p):
        cursor = p.cursor or {"open": []}
        now_open = open_incidents(api, settings)
        gone = [iid for iid in cursor["open"] if iid not in {i["id"] for i in now_open}]
        for i in now_open:
            if i["id"] not in cursor["open"]:
                p.counter("pagerduty_incidents_triggered_total", 1, {"urgency": i["urgency"] or ""},
                          help="PagerDuty incidents first seen open, by urgency.")
        # a closed incident's last state is read fresh (ttl 0): a cached open state would hide the change
        closed = [api.get(f"/incidents/{iid}", keep=keep_one, ttl_s=0) for iid in gone]
        for i in closed:
            p.counter("pagerduty_incidents_resolved_total", 1, {"urgency": i["urgency"] or ""},
                      help="PagerDuty incidents that stopped being open, by urgency.")
        for i in now_open + closed:
            p.emit(f"pagerduty.incident.{i['status']}", i["id"],
                   {"number": i["number"], "urgency": i["urgency"], "service": i["service"]},
                   source=actor_source("pagerduty", i["by"]), stamp=i["changed_at"], time=i["changed_at"])
        p.cursor = {"open": sorted(i["id"] for i in now_open)}

    return poll


def measure(api, settings, series) -> None:
    counts = dict.fromkeys(URGENCIES, 0)
    for i in open_incidents(api, settings):
        counts[i["urgency"] or ""] = counts.get(i["urgency"] or "", 0) + 1
    for urgency, n in sorted(counts.items()):
        series.gauge("pagerduty_incidents_open", n, {"urgency": urgency},
                     help="Open (triggered or acknowledged) PagerDuty incidents, by urgency (a level).")
    series.load_counters(counters_at(settings.poll_dir))


def main(argv: list[str] | None = None) -> int:
    return runner.main(HERE, make_poll, argv, measure=measure, auth_format="Token token={token}", headers=HEADERS)


if __name__ == "__main__":
    sys.exit(main())
