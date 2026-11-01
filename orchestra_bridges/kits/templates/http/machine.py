"""{{name}}: a poller for a JSON API no kit knows (scaffolded from the orchestra-bridges kit
``http``; edit freely, AGENTS.md says what it may do).

``python {{name}}.py`` (the poller): each poll reads the list endpoint (``[settings] list_path``),
takes each item's id (``key``), change stamp (``stamp``), state (``state``) and who changed it
(``actor``), and publishes ``<system>.<thing>.<state>`` once per stamp, as that actor
(``person.<system>.<account>``, ``bot.<system>`` when no one), with the item's id as the subject.

``python {{name}}.py measure`` (for a ``[[collect]]`` entry): prints ``<system>_<thing>s{state}``,
the items listed by state (one gauge).

It keeps metadata only: what ``keep_items`` takes (ids, stamps, states, accounts, times). Never a
title, description, log or a person's name: if the API puts text in a field you read, don't read it."""

from __future__ import annotations

import re
import sys
from pathlib import Path
from typing import Any

from orchestra_bridges.kits import runner
from orchestra_bridges.kits.poller import actor_source

HERE = Path(__file__).resolve().parent
WORD = re.compile(r"[^a-z0-9_]+")


def at(obj: Any, path: str) -> Any:
    """``obj``'s value at a dotted ``path`` (``""``: obj itself); None when it is not there."""
    for part in [p for p in path.split(".") if p] if path else []:
        obj = obj.get(part) if isinstance(obj, dict) else None
    return obj


def word(v: Any) -> str:
    return WORD.sub("_", str(v).lower()).strip("_") or "unknown"


def keeper(s: dict):
    """What is kept of an answer: per item, only the fields the settings name, as strings."""
    def keep(body: Any) -> dict:
        items = at(body, s.get("items", "items"))
        out = []
        for i in items if isinstance(items, list) else []:
            bot = at(i, s["actor_is_bot"]) if s.get("actor_is_bot") else False
            out.append({"key": str(at(i, s.get("key", "id"))), "stamp": str(at(i, s.get("stamp", "updated_at"))),
                        "state": word(at(i, s.get("state", "status"))),
                        "actor": (str(a) if (a := at(i, s["actor"])) not in (None, "") else None) if s.get("actor") else None,
                        "bot": bool(bot), "time": str(t) if s.get("time") and (t := at(i, s["time"])) else None})
        return {"items": out}
    return keep


def items(api, settings, ttl_s: float | None = None) -> list[dict]:
    s = settings.extra
    return api.get(s.get("list_path", "/api/items"), dict(s.get("params") or {}), keep=keeper(s), ttl_s=ttl_s)["items"]


def make_poll(api, settings):
    system, thing = word(settings.extra.get("system", "example")), word(settings.extra.get("thing", "item"))

    def poll(p):
        for i in items(api, settings, ttl_s=0):        # read fresh: a cached answer would hide a change
            p.emit(f"{system}.{thing}.{i['state']}", i["key"], {"state": i["state"]},
                   source=actor_source(system, i["actor"], bot=i["bot"]), stamp=i["stamp"], time=i["time"])

    return poll


def measure(api, settings, series) -> None:
    system, thing = word(settings.extra.get("system", "example")), word(settings.extra.get("thing", "item"))
    counts: dict[str, int] = {}
    for i in items(api, settings):
        counts[i["state"]] = counts.get(i["state"], 0) + 1
    for state, n in sorted(counts.items()):
        series.gauge(f"{system}_{thing}s", n, {"state": state}, help=f"{system} {thing}s listed, by state.")


def main(argv: list[str] | None = None) -> int:
    # auth_format: "Bearer {token}" (the default), "Token token={token}" (PagerDuty), "Basic {token}"
    # (the token's variable holding base64 of user:api_token, as Jenkins wants)
    return runner.main(HERE, make_poll, argv, measure=measure, auth_format="Bearer {token}")


if __name__ == "__main__":
    sys.exit(main())
