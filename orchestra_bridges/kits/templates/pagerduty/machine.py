"""{{name}}: a poller that relays PagerDuty's incident changes (scaffolded from the
orchestra-bridges kit ``pagerduty``; edit freely, AGENTS.md says what it may do).

``python {{name}}.py`` (the poller): each poll lists the open incidents (triggered and
acknowledged) and reads by id each incident that was open last poll and is not now (resolved,
usually). It publishes ``pagerduty.incident.<status>`` once per change, as whoever changed it
(``person.pagerduty.<user id>``, or ``bot.pagerduty`` when the service did), with the incident's id
as the subject and its ``last_status_change_at`` as the stamp.

``python {{name}}.py measure`` (for a ``[[collect]]`` entry): prints
``pagerduty_incidents_open{service, urgency}`` and ``pagerduty_incidents_open_total``.

It keeps metadata only: ids, numbers, states, times, the service's and the user's ids. Never a
title, summary or name."""

from __future__ import annotations

import sys
from pathlib import Path

from orchestra_bridges.kits import runner
from orchestra_bridges.kits.poller import actor_source

HERE = Path(__file__).resolve().parent
PAGE = 100
HEADERS = {"Accept": "application/vnd.pagerduty+json;version=2"}


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
        # a closed incident's last state is read fresh (ttl 0): a cached open state would hide the change
        closed = [api.get(f"/incidents/{iid}", keep=keep_one, ttl_s=0) for iid in gone]
        for i in now_open + closed:
            p.emit(f"pagerduty.incident.{i['status']}", i["id"],
                   {"number": i["number"], "urgency": i["urgency"], "service": i["service"]},
                   source=actor_source("pagerduty", i["by"]), stamp=i["changed_at"], time=i["changed_at"])
        p.cursor = {"open": sorted(i["id"] for i in now_open)}

    return poll


def measure(api, settings, series) -> None:
    now_open = open_incidents(api, settings)
    counts: dict[tuple, int] = {}
    for i in now_open:
        k = (i["service"] or "", i["urgency"] or "")
        counts[k] = counts.get(k, 0) + 1
    for (service, urgency), n in sorted(counts.items()):
        series.gauge("pagerduty_incidents_open", n, {"service": service, "urgency": urgency},
                     help="Open (triggered or acknowledged) PagerDuty incidents.")
    series.gauge("pagerduty_incidents_open_total", len(now_open), help="Open PagerDuty incidents, all services.")


def main(argv: list[str] | None = None) -> int:
    return runner.main(HERE, make_poll, argv, measure=measure, auth_format="Token token={token}", headers=HEADERS)


if __name__ == "__main__":
    sys.exit(main())
