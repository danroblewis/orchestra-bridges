"""The table that combines accounts into people (SPEC §8): candidates from what the relays
announced, across systems, each with its evidence and confidence; proposals only."""

import json

from conftest import new_project
from orchestra.events import Events

from orchestra_bridges.__main__ import main as bridges_main
from orchestra_bridges.identity import People, join, state_map
from orchestra_bridges.people_table import propose, seen


def announced(source, **data):
    return {"type": "orchestra.machine.started", "source": source,
            "data": {"kind": "person", "mode": "ad hoc", "package": "bridges", **data}}


TWO_SYSTEMS = [
    announced("person.jira.alice", name="jira.user.alice", display_name="Alice Example"),
    announced("person.slack.u012ab3cd", name="slack.user.u012ab3cd", display_name="alice example"),   # exact
    announced("person.jira.bob.b", name="jira.user.bob.b", display_name="Bob Builder"),
    announced("person.github.bob-b", name="github.user.bob-b", display_name="Builder, Bob"),          # weak
    announced("person.github.carol", name="github.user.carol"),                                     # nothing
]


def test_two_systems_give_one_exact_and_one_weak_candidate():
    found = propose(seen(TWO_SYSTEMS))
    assert [(c.strength, sorted(a.source for a in c.accounts)) for c in found] == [
        ("exact", ["person.jira.alice", "person.slack.u012ab3cd"]),
        ("weak", ["person.github.bob-b", "person.jira.bob.b"])]
    exact, weak = found
    assert exact.confidence == 0.9 and exact.key == "alice_example" and exact.name in ("Alice Example", "alice example")
    assert "the same display name" in exact.evidence[0]
    assert weak.confidence == 0.7 and "another order" in weak.evidence[0]
    assert exact.entry()["accounts"] == {"jira": "alice", "slack": "u012ab3cd"}
    assert propose(seen(TWO_SYSTEMS))[0].id == exact.id                 # the same accounts, the same id


def test_handles_and_names_are_weak_and_an_opaque_id_is_no_handle():
    found = propose(seen([
        announced("person.jira.dana", name="jira.user.dana", display_name="Dana Doe"),
        announced("person.github.dana", name="github.user.dana"),                    # the same handle
        announced("person.github.ddoe", name="github.user.ddoe"),                    # made from the name
        announced("person.slack.dana", name="slack.user.dana"),                      # a member id: no handle
        announced("person.jira.5b10ac8d82e05b22cc7d4ef5", name="jira.user.erin_e", name_derived="display_name",
                  display_name="Erin E"),
        announced("person.github.5b10ac8d82e05b22cc7d4ef5", name="github.user.x")]))
    got = {tuple(sorted(a.source for a in c.accounts)): (c.strength, c.confidence) for c in found}
    assert got == {("person.github.dana", "person.jira.dana"): ("weak", 0.6),
                   ("person.github.ddoe", "person.jira.dana"): ("weak", 0.5)}


def test_namesakes_on_one_system_make_the_match_weak_and_the_map_keeps_its_own_out():
    events = [announced("person.jira.alex1", display_name="Alex Kim"), announced("person.jira.alex2", display_name="Alex Kim"),
              announced("person.slack.u1", display_name="Alex Kim"),
              announced("person.jira.alice", display_name="Alice Example"), announced("person.slack.u2", display_name="Alice Example")]
    [namesakes, *rest] = propose(seen(events), People({"alice": {"name": "Alice Example", "jira": "alice"}}))
    assert namesakes.strength == "weak" and namesakes.confidence == 0.4 and "namesakes" in namesakes.evidence[-1]
    assert len(namesakes.accounts) == 3 and not rest                     # alice is joined already: no candidate


def test_an_announcement_from_before_b10_gives_its_name_as_the_display_name():
    [old] = seen([announced("person.jira.alice", name="Alice Example")])
    assert old.display_name == "Alice Example" and old.handle == "alice"
    [new] = seen([announced("person.jira.alice", name="Alice Example"), announced("person.jira.alice", name="jira.user.alice")])
    assert new.display_name is None                                     # the newest announcement


def test_the_command_lists_the_buss_candidates_and_writes_nothing(tmp_path, monkeypatch, capsys):
    proj = new_project(tmp_path, monkeypatch, "")
    events = Events.for_project(proj)
    for e in TWO_SYSTEMS:
        events.publish(e["type"], e["data"], source=e["source"])
    assert bridges_main(["people", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert [r["strength"] for r in rows] == ["exact", "weak"] and rows[0]["evidence"]
    assert not state_map(proj.state_dir).exists()                       # proposals only
    join(state_map(proj.state_dir), [{"key": "alice", "name": "Alice Example", "accounts": {"jira": "alice", "slack": "u012ab3cd"}}])
    assert bridges_main(["people"]) == 0
    out = capsys.readouterr().out
    assert "weak 0.70" in out and "exact" not in out                    # the joined pair is no longer proposed
