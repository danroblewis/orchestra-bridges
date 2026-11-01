"""http_example against a fake of its API written here, on the kits' ``FakeServer`` (a stdlib HTTP server
on a random loopback port that records requests and checks a token): two polls (a restart between
them) relay each item's change once, as who made it; ``measure`` prints the items by state; no
text or token leaves. Change ``FakeAPI`` to answer in your API's real shapes.

Run with Orchestra's own Python (the one ``orchestra`` runs in, which has the kits): ``python -m pytest .orchestra/machines/http_example/test_http_example.py``."""

import importlib.util
import json
from pathlib import Path

import pytest

from orchestra_bridges.kits.fakes import CANARY, FakeServer
from orchestra_bridges.kits.testing import fake_token, set_args, token_env, uncleared

HERE = Path(__file__).resolve().parent
TOKEN = fake_token(HERE)        # the placeholder the runner sends a fake on this machine
REAL = "real-token-never-sent-to-a-fake"   # in the environment, yet never read for a fake
TOKEN_ENV = token_env(HERE, "API_TOKEN")         # poller.toml's, so the owner's variable works too
# The shape FakeAPI answers in. When you change FakeAPI to your API's real shapes, change these to
# match (or drop them, and the run reads poller.toml's own).
SCOPE = set_args(system="example", thing="item", list_path="/api/items", params={}, items="items", key="id",
                 stamp="updated_at", state="status", actor="updated_by", actor_is_bot="", time="updated_at")


class FakeAPI(FakeServer):
    """The list endpoint of poller.toml's settings, in the shape they read; every text has the canary."""

    def route(self, method, path, query, body):
        if method == "GET" and path == "/api/items":
            return 200, {"items": self.data["items"]}
        return 404, {"error": "not found"}

    def change(self, id, status, by, at):
        for i in self.data["items"]:
            if i["id"] == id:
                i.update(status=status, updated_by=by, updated_at=at)


WORLD = {"items": [
    {"id": "1", "status": "running", "updated_at": "2026-11-01T08:00:00Z", "updated_by": "alice",
     "title": f"{CANARY} one"},
    {"id": "2", "status": "queued", "updated_at": "2026-11-01T08:01:00Z", "updated_by": None,
     "title": f"{CANARY} two"},
]}


def program(here=HERE):
    spec = importlib.util.spec_from_file_location("http_example", here / "http_example.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def fake(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCHESTRATOR_HOME", str(tmp_path / "home"))
    monkeypatch.setenv(TOKEN_ENV, REAL)
    with FakeAPI(WORLD, token=TOKEN) as f:
        yield f


def args(fake, tmp, *extra):
    return [*extra, "--base-url", fake.url, "--state-dir", str(tmp / "state"), "--ttl", "0", *SCOPE]


def poll_once(fake, tmp):
    assert program().main(args(fake, tmp, "--once", "--events-file", str(tmp / "events.jsonl"))) == 0
    return [json.loads(x) for x in (tmp / "events.jsonl").read_text().splitlines()]


def changes(events):
    return [(e["type"], e["subject"], e["source"]) for e in events if e["type"] != "orchestra.machine.started"]


def test_two_polls_relay_each_change_once(fake, tmp_path):
    assert changes(poll_once(fake, tmp_path)) == [("example.item.running", "1", "person.example.alice"),
                                                  ("example.item.queued", "2", "bot.example")]
    fake.change("1", "passed", "bob", "2026-11-01T08:05:00Z")
    events = poll_once(fake, tmp_path)                                  # a restart: the emitted ids are read back
    assert changes(events)[2:] == [("example.item.passed", "1", "person.example.bob")]
    assert sorted(e["source"] for e in events if e["type"] == "orchestra.machine.started") == [
        "bot.example", "person.example.alice", "person.example.bob"]
    assert not any(e["source"] == "http_example" for e in events)
    assert all(r.authorized for r in fake.requests)


def test_measure_prints_the_items_by_state(fake, tmp_path, capsys):
    assert program().main(args(fake, tmp_path, "measure")) == 0
    out = capsys.readouterr().out
    assert 'example_items{state="running"} 1' in out and 'example_items{state="queued"} 1' in out


def test_no_text_or_token_leaves(fake, tmp_path, capsys):
    poll_once(fake, tmp_path)
    program().main(args(fake, tmp_path, "measure"))
    out = capsys.readouterr()
    assert CANARY not in out.out + out.err and REAL not in out.out + out.err
    for p in tmp_path.rglob("*"):
        if p.is_file():
            text = p.read_text(errors="replace")
            assert CANARY not in text and REAL not in text, p


def test_refuses_an_uncleared_system(tmp_path, monkeypatch):
    monkeypatch.setenv(TOKEN_ENV, REAL)
    copy = uncleared(HERE, tmp_path / "copy", "http_example.py")      # holds after the owner's approval too
    rc = program(copy).main(["--once", "--base-url", "https://ci.example.com", "--state-dir", str(tmp_path / "s"),
                         "--events-file", str(tmp_path / "e.jsonl")])
    assert rc == 2 and not (tmp_path / "e.jsonl").exists()
