"""The Slack bridge's parts: the client, reading a search match, the token, the settings, the
plugin's declaration."""

import os
import sys
from pathlib import Path

import pytest
from fake_slack import CANARY, CHANNEL, OWNER, TEAM, TOKEN, FakeSlack
from orchestra.config import Config, ConfigError, Project

from orchestra_bridges import config as cfg
from orchestra_bridges.plugin import plugin
from orchestra_bridges.slack import search
from orchestra_bridges.slack.api import Slack, SlackError
from orchestra_bridges.slack.pings import iso_ts, parse_id
from orchestra_bridges.slack.token import TokenError, user_token
from orchestra_bridges.slack.waits import Waits


def test_auth_and_a_wrong_token_named_by_slacks_error_never_the_token():
    with FakeSlack() as s:
        assert Slack(TOKEN, s.base).auth()["user_id"] == OWNER
        with pytest.raises(SlackError) as e:
            Slack("wrong-fake-token-xyz", s.base).auth()
        assert str(e.value) == "auth.test: invalid_auth" and "wrong-fake-token-xyz" not in repr(Slack("wrong-fake-token-xyz"))
        assert s.requests[0][2] == f"Bearer {TOKEN}"


def test_retry_after_is_waited_out_and_a_lasting_limit_fails_the_call():
    waits = []
    with FakeSlack() as s:
        s.fail_next = [(429, {"Retry-After": "7"}), (429, {"Retry-After": "2"})]
        api = Slack(TOKEN, s.base, sleep=waits.append)
        assert api.auth()["ok"] and waits == [7.0, 2.0] and api.requests == 3
        s.fail_next = [(429, {"Retry-After": "1"})] * 4
        with pytest.raises(SlackError, match="ratelimited"):
            api.auth()
        s.fail_next = [(500, {})]
        with pytest.raises(SlackError, match="HTTP 500"):
            api.auth()


def test_the_token_never_goes_to_another_host():
    import http.server
    import threading

    class Redirect(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            self.send_response(302)
            self.send_header("Location", "http://evil.example/api/auth.test")
            self.end_headers()

    srv = http.server.HTTPServer(("127.0.0.1", 0), Redirect)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    try:
        with pytest.raises(SlackError, match="refused"):
            Slack(TOKEN, f"http://127.0.0.1:{srv.server_address[1]}/api").auth()
    finally:
        srv.shutdown()
        srv.server_close()


def test_a_match_keeps_metadata_only_and_finds_its_thread():
    s = FakeSlack()
    s.user("U0BOB", "bob")
    root = s.say("U0BOB", CHANNEL, 1000.0)
    reply = s.say("U0BOB", CHANNEL, 1010.0, mentions=(OWNER, "S0SRE"), thread=root)
    m = search.message(s.match(s.messages[1]), {OWNER, "S0SRE", "S0OTHER"})
    assert (m.channel, m.ts, m.thread_ts, m.thread, m.user, m.username, m.channel_type) == (
        CHANNEL, reply, root, root, "U0BOB", "bob", "channel")
    assert m.mentions == {OWNER, "S0SRE"}
    assert CANARY not in repr(m)
    top = search.message(s.match(s.messages[0]), {OWNER})
    assert top.thread_ts is None and top.thread == root and not top.mentions
    assert search.message({"ts": "1.0", "channel": {}}, set()) is None
    given = search.message({**s.match(s.messages[1]), "thread_ts": root, "permalink": ""}, set())
    assert given.thread_ts == root                     # Slack's own thread_ts, when a match has it


def test_ids_and_times():
    assert parse_id(f"slack:{TEAM}:answered:{CHANNEL}:1793000000.000100") == (TEAM, "slack.ping.answered", CHANNEL,
                                                                              "1793000000.000100")
    assert parse_id(f"slack:announce:person.bob:abc:{TEAM}") is None
    assert iso_ts("1793000000.0001") == "2026-10-26T07:33:20.000100+00:00"


def test_waits_balance_and_continue_from_the_larger():
    w = Waits()
    w.fold("slack.ping.received", "C", "1.0", "s", 1.0)
    w.fold("slack.ping.received", "C", "2.0", "s", 2.0)
    assert w.fold("slack.ping.received", "C", "2.0", "s", 2.0) is None
    assert w.fold("slack.ping.answered", "C", "1.0", "s", 901.0) == 900.0
    assert w.fold("slack.ping.answered", "C", "1.0", "s", 999.0) is None
    w.fold("slack.ping.expired", "C", "2.0", "s", 50.0)
    assert (w.received, w.answered["15m"], w.expired, len(w.waiting)) == (2, 1, 1, 0)
    w.continue_from({"received": 5.0, "expired": 0.0})
    vals = {(n, tuple(e.items())): v for n, e, v in w.values(1000.0)}
    assert vals[("slack_pings_received_total", ())] == 5.0 and vals[("slack_pings_expired_total", ())] == 1.0


def proj(toml: str) -> Project:
    return Project("k", Path("/repo"), Config.from_toml(toml))


def test_slack_settings():
    c = cfg.slack(proj("[bridges.slack]\n").config.raw, Path("/repo"))
    assert (c.every, c.answered, c.reactions, c.groups, c.dms, c.expire, c.port, c.host, c.token_file, c.api) == (
        60.0, ["reaction"], ["white_check_mark", "heavy_check_mark"], [], True, 604800.0, 9465, "127.0.0.1", None,
        "https://slack.com/api")
    c = cfg.slack(proj('[bridges.slack]\nanswered = "reply"\nreactions = [":ok:"]\ngroups = ["S1"]\n'
                       'token_file = "~/t"\nqueries = ["<@{me}> in:#ops"]\n').config.raw, Path("/repo"))
    assert (c.answered, c.reactions, c.groups, c.token_file, c.queries) == (
        ["reply"], ["ok"], ["S1"], Path("~/t").expanduser(), ["<@{me}> in:#ops"])


@pytest.mark.parametrize("api", ["https://slack.com/api", "https://acme.enterprise.slack.com/api/",
                                 "http://127.0.0.1:8123/api", "http://localhost:9/api", "http://[::1]:9/api"])
def test_the_token_goes_only_to_slack_or_a_local_fake(api):
    assert cfg.slack(proj(f'[bridges.slack]\napi = "{api}"\n').config.raw, Path("/repo")).api == api.rstrip("/")


@pytest.mark.parametrize("toml", ['[bridges.slack]\ntoken = "xyz"\n', '[bridges.slack]\nanswered = ["emoji"]\n',
                                  '[bridges.slack]\ngroups = ["sre"]\n', '[bridges.slack]\nevery = "1s"\n',
                                  '[bridges.slack]\nport = "x"\n', '[bridges.slack]\nanswered = []\n',
                                  '[bridges.slack]\napi = "http://slack.com/api"\n',
                                  '[bridges.slack]\napi = "https://slack.com.evil.example/api"\n',
                                  '[bridges.slack]\napi = "https://evil.example/api"\n',
                                  '[bridges.slack]\napi = "http://10.0.0.5:8080/api"\n', ""])
def test_bad_slack_settings_are_refused(toml):
    with pytest.raises(ConfigError):
        cfg.slack(proj(toml).config.raw, Path("/repo"))


def test_the_token_comes_from_the_environment_or_a_private_file_outside_the_repository(tmp_path, monkeypatch):
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.delenv("SLACK_USER_TOKEN_FILE", raising=False)
    monkeypatch.setenv("SLACK_USER_TOKEN", TOKEN)
    assert user_token(repo) == TOKEN
    monkeypatch.delenv("SLACK_USER_TOKEN")
    f = tmp_path / "slack-token"
    f.write_text(TOKEN + "\n")
    os.chmod(f, 0o600)
    assert user_token(repo, f) == TOKEN
    os.chmod(f, 0o644)
    with pytest.raises(TokenError, match="chmod 600"):
        user_token(repo, f)
    inside = repo / "token"
    inside.write_text(TOKEN)
    os.chmod(inside, 0o600)
    monkeypatch.setenv("SLACK_USER_TOKEN_FILE", str(inside))
    with pytest.raises(TokenError, match="inside the repository"):
        user_token(repo)
    monkeypatch.setenv("SLACK_USER_TOKEN_FILE", str(tmp_path / "none"))
    with pytest.raises(TokenError, match="no SLACK_USER_TOKEN") as e:
        user_token(repo)
    assert TOKEN not in str(e.value)


def test_the_plugin_runs_the_slack_bridge_where_it_is_configured():
    on = plugin(proj("[bridges.slack]\n[bridges.github]\n"))
    assert [(p.name, p.argv()) for p in on.processes] == [
        ("github", [sys.executable, "-m", "orchestra_bridges", "github"]),
        ("slack", [sys.executable, "-m", "orchestra_bridges", "slack"])]
    assert [m.id for m in on.machines] == ["bridges.github", "bridges.slack"]
    only = plugin(proj("[bridges.slack]\n"))
    assert [p.name for p in only.processes] == ["slack"]


def test_the_plugin_declares_the_scrape_that_stores_the_slack_bridges_counts():
    from dataclasses import replace

    from orchestra.collect.spec import from_toml

    from orchestra_bridges.slack.bridge import INSTRUMENT

    def instruments(toml: str):
        p = plugin(proj(toml))
        found, errors = from_toml(proj(toml).config.raw, [replace(c, owner=p.name) for c in p.collectors])
        return {c.name: (c.target, c.owner) for c in found}, errors

    assert plugin(proj("[bridges.github]\n")).collectors == []
    got, errors = instruments("[bridges.slack]\n")
    assert got == {INSTRUMENT: ("http://127.0.0.1:9465/metrics", "bridges")} and not errors   # the name it reads back by
    assert instruments('[bridges.slack]\nhost = "0.0.0.0"\nport = 9500\n')[0] == {
        INSTRUMENT: ("http://127.0.0.1:9500/metrics", "bridges")}
    assert instruments('[bridges.slack]\nhost = "::"\n')[0][INSTRUMENT][0] == "http://[::1]:9465/metrics"
    assert plugin(proj("[bridges.slack]\nport = 0\n")).collectors == []                      # no fixed port to scrape
    got, errors = instruments('[bridges.slack]\n[[scrape]]\nname = "bridges.slack"\nurl = "http://127.0.0.1:9999/metrics"\n')
    assert got == {INSTRUMENT: ("http://127.0.0.1:9999/metrics", "project")}                  # a hand-made entry wins
    assert errors == ["plugin bridges: its collector bridges.slack is replaced by the project's entry of that name"]
