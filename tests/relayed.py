"""The rule every bridge test checks after a run: a bridge is a relay, never a machine (Orchestra's
AGENTS.md rule 2, D19). Nothing on the bus has a ``bridges.*`` source, no announcement comes from a
bridge, and Orchestra's Machines view (``MachineIndex``) lists only people and bots beside its own
host."""

from orchestra.events import HOST
from orchestra.web.machines import machines_view

KIND = {"person.": "person", "bot.": "program"}


def only_actors(evs: list[dict]) -> list[dict]:
    """Assert the rule over ``evs`` (a bus read back); the machines the view lists, for more checks."""
    assert evs, "nothing was published"
    sources = {e.get("source") or "" for e in evs}
    assert not [s for s in sources if s.startswith("bridges")], sources
    assert all(s.startswith(tuple(KIND)) for s in sources), sources
    assert all(e["source"].startswith(tuple(KIND)) for e in evs if e["type"] == "orchestra.machine.started")
    machines = [m for m in machines_view(evs)["machines"] if m["id"] != HOST]
    assert machines
    for m in machines:
        prefix = next(p for p in KIND if m["id"].startswith(p))
        assert m["kind"] == KIND[prefix] and m["package"] == "bridges", m
    return machines
