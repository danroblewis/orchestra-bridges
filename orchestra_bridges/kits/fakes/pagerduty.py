"""A fake of PagerDuty's REST API v2 (``https://api.pagerduty.com``), in its shapes, for what a
poller reads: ``GET /incidents`` (``statuses[]``, ``service_ids[]``, ``urgencies[]``, ``since`` and
``until`` on the creation time, ``offset``/``limit`` paging with ``more``) and ``GET
/incidents/<id>``, each incident with ``last_status_change_by`` (a user, or its service). The token goes as ``Authorization: Token token=<token>``. Every title carries
the canary. ``add_incident`` and ``set_status`` change the world between polls."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from .base import CANARY, FakeServer, one

STATUSES = ("triggered", "acknowledged", "resolved")


def _t(s: str) -> float:
    return datetime.fromisoformat(s).timestamp()


def iso(t: float) -> str:
    return datetime.fromtimestamp(t, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


class FakePagerDuty(FakeServer):
    fixture_name = "pagerduty.json"
    auth_format = "Token token={token}"

    def _service(self, sid: str) -> dict:
        s = next((x for x in self.data.get("services", []) if x["id"] == sid), {"id": sid, "summary": sid})
        return {"id": s["id"], "type": "service_reference", "summary": s.get("summary", sid),
                "self": f"https://api.pagerduty.com/services/{s['id']}"}

    def _by(self, i: dict) -> dict:
        """Who changed its status last: a user, or the service itself (a trigger by an integration)."""
        uid = i.get("changed_by")
        if not uid:
            return self._service(i["service"])
        u = next((x for x in self.data.get("users", []) if x["id"] == uid), {"id": uid, "summary": uid})
        return {"id": u["id"], "type": "user_reference", "summary": u["summary"],
                "self": f"https://api.pagerduty.com/users/{u['id']}"}

    def _shape(self, i: dict) -> dict:
        return {
            "id": i["id"], "type": "incident", "incident_number": i["incident_number"], "title": i["title"],
            "summary": f"[#{i['incident_number']}] {i['title']}", "status": i["status"], "urgency": i["urgency"],
            "created_at": i["created_at"], "last_status_change_at": i["last_status_change_at"],
            "updated_at": i.get("updated_at", i["last_status_change_at"]),
            "service": self._service(i["service"]),
            "last_status_change_by": self._by(i),
            "self": f"https://api.pagerduty.com/incidents/{i['id']}",
            "html_url": f"https://acme.pagerduty.com/incidents/{i['id']}",
        }

    # the test's controls
    def add_incident(self, id: str, service: str = "PSVC001", status: str = "triggered", urgency: str = "high",
                     at: str | None = None) -> None:
        with self._lock:
            n = 100 + len(self.data["incidents"]) + 1
            at = at or iso(datetime.now(UTC).timestamp())
            self.data["incidents"].append({"id": id, "incident_number": n, "title": f"{CANARY}: incident {n}",
                                           "status": status, "urgency": urgency, "created_at": at,
                                           "last_status_change_at": at, "service": service, "changed_by": None})

    def set_status(self, id: str, status: str, by: str | None = None, at: str | None = None) -> None:
        """``by``: the user who changed it (None: the service, as an auto-resolve)."""
        if status not in STATUSES:
            raise ValueError(status)
        with self._lock:
            i = next(x for x in self.data["incidents"] if x["id"] == id)
            i["status"], i["last_status_change_at"] = status, at or iso(datetime.now(UTC).timestamp())
            i["changed_by"] = by

    def route(self, method: str, path: str, query: dict[str, list[str]], body: Any) -> tuple[int, Any]:
        if method != "GET":
            return 405, {"error": {"message": "Method Not Allowed", "code": 2100}}
        if path == "/incidents":
            return self._list(query)
        if path.startswith("/incidents/"):
            iid = path.rsplit("/", 1)[1]
            for i in self.data["incidents"]:
                if i["id"] == iid:
                    return 200, {"incident": self._shape(i)}
            return 404, {"error": {"message": "Not Found", "code": 2100}}
        return 404, {"error": {"message": "Not Found", "code": 2100}}

    def _list(self, q: dict[str, list[str]]) -> tuple[int, Any]:
        statuses = q.get("statuses[]") or []
        if bad := [s for s in statuses if s not in STATUSES]:
            return 400, {"error": {"message": "Invalid Input Provided", "code": 2001, "errors": [f"statuses {bad}"]}}
        services, urgencies = q.get("service_ids[]") or [], q.get("urgencies[]") or []
        since, until = one(q, "since"), one(q, "until")
        try:
            lo = _t(since) if since else None
            hi = _t(until) if until else None
            offset, limit = int(one(q, "offset", "0")), min(int(one(q, "limit", "25")), 100)
        except ValueError:
            return 400, {"error": {"message": "Invalid Input Provided", "code": 2001}}
        rows = [i for i in self.data["incidents"]
                if (not statuses or i["status"] in statuses) and (not services or i["service"] in services)
                and (not urgencies or i["urgency"] in urgencies)
                and (lo is None or _t(i["created_at"]) >= lo) and (hi is None or _t(i["created_at"]) < hi)]
        rows.sort(key=lambda i: i["created_at"])
        page = rows[offset:offset + limit]
        return 200, {"incidents": [self._shape(i) for i in page], "limit": limit, "offset": offset,
                     "more": offset + limit < len(rows), "total": None}
