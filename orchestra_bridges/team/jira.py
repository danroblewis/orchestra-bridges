"""The scripted team's acts in Jira, in either flavour: Cloud (REST v3, account ids, the Atlassian
document format for text) or Data Center (REST v2, user names, plain text). A move is by stage
(``start``: a status of the in-progress category that is not a review; ``review``: one whose name
says review; ``done``: the done category), through the transitions the ticket has now."""

from __future__ import annotations

from .wire import Wire, WireError


def adf(text: str) -> dict:
    return {"type": "doc", "version": 1, "content": [{"type": "paragraph", "content": [{"type": "text", "text": text}]}]}


class Jira:
    def __init__(self, wire: Wire, cloud: bool):
        self.wire, self.cloud = wire, cloud
        self.api = "/rest/api/3" if cloud else "/rest/api/2"

    def _text(self, text: str):
        return adf(text) if self.cloud else text

    def _who(self, person: dict) -> str:
        return person.get("accountId") if self.cloud else person.get("name")

    def people(self, project: str) -> list[str]:
        """The project's assignable people (Cloud: Atlassian accounts only, never an app)."""
        got = self.wire.call("GET", f"{self.api}/user/assignable/search", query={"project": project, "maxResults": 50})
        return [self._who(u) for u in got or [] if not self.cloud or u.get("accountType", "atlassian") == "atlassian"]

    def create(self, project: str, kind: str, summary: str, labels: list[str]) -> tuple[str, str]:
        made = self.wire.call("POST", f"{self.api}/issue", {"fields": {
            "project": {"key": project}, "issuetype": {"name": kind}, "summary": summary, "labels": labels,
            "description": self._text("Created by the scripted team (orchestra_bridges.team) to test read-only pollers.")}})
        return made["key"], made["id"]

    def assign(self, key: str, who: str) -> None:
        self.wire.call("PUT", f"{self.api}/issue/{key}/assignee", {"accountId": who} if self.cloud else {"name": who})

    def comment(self, key: str, text: str) -> None:
        self.wire.call("POST", f"{self.api}/issue/{key}/comment", {"body": self._text(text)})

    def issue(self, key: str) -> dict:
        """The ticket's summary, labels and status category (what ``--close`` checks before it acts)."""
        f = (self.wire.call("GET", f"{self.api}/issue/{key}", query={"fields": "summary,labels,status"}) or {}).get("fields") or {}
        return {"summary": f.get("summary") or "", "labels": list(f.get("labels") or []),
                "status": (f.get("status") or {}).get("name"),
                "category": ((f.get("status") or {}).get("statusCategory") or {}).get("key")}

    def transitions(self, key: str) -> list[dict]:
        got = self.wire.call("GET", f"{self.api}/issue/{key}/transitions") or {}
        return [{"id": t["id"], "to": (t.get("to") or {}).get("name") or t.get("name"),
                 "category": ((t.get("to") or {}).get("statusCategory") or {}).get("key")}
                for t in got.get("transitions") or []]

    @staticmethod
    def pick(transitions: list[dict], stage: str) -> dict | None:
        if stage == "done":
            return next((t for t in transitions if t["category"] == "done"), None)
        doing = [t for t in transitions if t["category"] == "indeterminate"]
        if stage == "review":
            return next((t for t in doing if "review" in (t["to"] or "").lower()), None)
        return next((t for t in doing if "review" not in (t["to"] or "").lower()), None)

    def move(self, key: str, stage: str) -> str | None:
        """Move to the stage; the status it reached, or None when no transition leads there now."""
        t = self.pick(self.transitions(key), stage)
        if t is None:
            return None
        self.wire.call("POST", f"{self.api}/issue/{key}/transitions", {"transition": {"id": t["id"]}})
        return t["to"]

    def close(self, key: str, resolution: str) -> tuple[str | None, str | None]:
        """Walk the ticket to a done status (at most four steps), setting the resolution when the
        transition's screen takes one; (the status reached, the resolution set) or (None, None)."""
        seen: set[str] = set()
        for _ in range(4):
            ts = self.transitions(key)
            done = self.pick(ts, "done")
            if done is not None:
                path = f"{self.api}/issue/{key}/transitions"
                try:
                    self.wire.call("POST", path, {"transition": {"id": done["id"]}, "fields": {"resolution": {"name": resolution}}})
                    return done["to"], resolution
                except WireError as e:
                    if e.status != 400:
                        raise
                    self.wire.call("POST", path, {"transition": {"id": done["id"]}})   # a screen without the field
                    return done["to"], None
            step = next((t for t in ts if t["category"] == "indeterminate" and t["to"] not in seen), None)
            if step is None:
                return None, None
            seen.add(step["to"])
            self.wire.call("POST", f"{self.api}/issue/{key}/transitions", {"transition": {"id": step["id"]}})
        return None, None
