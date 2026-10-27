"""The bridges' settings: sections of the repository's ``orchestra.toml`` that Orchestra ignores.

::

    [bridges]
    people = "people.toml"            # the identity map (identity.py), relative to the repository

    [bridges.github]
    repos = ["microsoft/vscode"]      # owner/name; the default when the section names none
    every = "60s"                     # how often to poll
    lookback = "1h"                   # on the very first start, how far back to read
    max_catchup = "48h"               # after a gap, how far back it will catch up at most
    overlap = "10m"                   # each poll re-reads this much before the last one
    bots_answer = false               # whether a bot's comment or close counts as an issue's first response
    api = "https://api.github.com"    # GitHub Enterprise, or a fake in tests

    [bridges.slack]
    every = "60s"                     # how often to poll search.messages
    lookback = "1h"                   # on the very first start, how far back to read
    max_catchup = "48h"               # after a gap, how far back it will catch up at most
    overlap = "10m"                   # each poll re-reads this much before the last one
    answered = ["reaction"]           # what answers a ping: "reaction" (a checkmark), "reply", or both; never none
    reactions = ["white_check_mark", "heavy_check_mark"]   # the reactions that answer
    groups = []                       # user group ids (S...) whose mentions are pings too
    dms = true                        # whether a direct message is a ping
    expire = "168h"                   # a ping unanswered this long stops waiting
    host = "127.0.0.1"                # where /metrics listens: local only unless you change it
    port = 9465                       # where /metrics is served, for a [[scrape]]
    token_file = "~/.config/orchestra-bridges/slack-token"   # never the token itself
    queries = ["<@{me}>", "to:<@{me}>"]   # replaces the default searches (slack/search.py); {me} is your member id
    api = "https://slack.com/api"     # https on slack.com, or a local fake (loopback) in tests

``[bridges] people`` (the identity map) applies to every bridge.

A bridge runs only where its section exists (``plugin.py``). The Slack token is never in
orchestra.toml: a ``token`` key there is refused (``slack/token.py``)."""

from __future__ import annotations

import ipaddress
import re
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from orchestra.config import ConfigError, parse_duration

REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


@dataclass
class GitHubConfig:
    repos: list[str] = field(default_factory=lambda: ["microsoft/vscode"])
    every: float = 60.0
    lookback: float = 3600.0
    max_catchup: float = 2 * 86400.0
    overlap: float = 600.0
    bots_answer: bool = False
    api: str = "https://api.github.com"
    people: Path | None = None


@dataclass
class SlackConfig:
    every: float = 60.0
    lookback: float = 3600.0
    max_catchup: float = 2 * 86400.0
    overlap: float = 600.0
    answered: list[str] = field(default_factory=lambda: ["reaction"])
    reactions: list[str] = field(default_factory=lambda: ["white_check_mark", "heavy_check_mark"])
    groups: list[str] = field(default_factory=list)
    dms: bool = True
    expire: float = 7 * 86400.0
    host: str = "127.0.0.1"
    port: int = 9465
    token_file: Path | None = None
    api: str = "https://slack.com/api"
    people: Path | None = None
    queries: list[str] | None = None             # the searches, when the defaults (search.py) are not right


def section(raw: dict[str, Any], name: str) -> dict | None:
    """``[bridges.<name>]`` of a parsed orchestra.toml, or None when the bridge is not configured."""
    s = (raw.get("bridges") or {}).get(name)
    return s if isinstance(s, dict) else None


def people_file(raw: dict[str, Any], repo_root: Path) -> Path | None:
    p = (raw.get("bridges") or {}).get("people")
    return (repo_root / p) if isinstance(p, str) and p else None


def github(raw: dict[str, Any], repo_root: Path) -> GitHubConfig:
    s = section(raw, "github")
    if s is None:
        raise ConfigError("orchestra.toml has no [bridges.github] section")
    repos = s.get("repos", ["microsoft/vscode"])
    if isinstance(repos, str):
        repos = [repos]
    bad = [r for r in repos if not isinstance(r, str) or not REPO.match(r)]
    if bad or not repos:
        raise ConfigError(f"[bridges.github] repos must be owner/name strings, not {bad or repos!r}")
    c = GitHubConfig(
        repos=list(repos),
        every=parse_duration(s.get("every"), 60.0),
        lookback=parse_duration(s.get("lookback"), 3600.0),
        max_catchup=parse_duration(s.get("max_catchup"), 2 * 86400.0),
        overlap=parse_duration(s.get("overlap"), 600.0),
        bots_answer=bool(s.get("bots_answer", False)),
        api=str(s.get("api") or "https://api.github.com").rstrip("/"),
        people=people_file(raw, repo_root),
    )
    if c.every < 5:
        raise ConfigError("[bridges.github] every must be at least 5s")
    return c


GROUP = re.compile(r"^S[A-Z0-9]+$")
RULES = ("reaction", "reply")


def _strings(s: dict, key: str, default: list[str]) -> list[str]:
    v = s.get(key, default)
    if isinstance(v, str):
        v = [v]
    if not isinstance(v, list) or not all(isinstance(x, str) and x for x in v):
        raise ConfigError(f"[bridges.slack] {key} must be a list of strings, not {v!r}")
    return list(v)


def _loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def slack_api(url: str) -> str:
    """``api`` when the owner's user token may go there: https on slack.com (or a host under it), or
    a fake on this machine (http or https on a loopback host). Anything else is refused: the token
    reads every one of the owner's messages, and orchestra.toml is committed."""
    u = urllib.parse.urlsplit(url)
    host = (u.hostname or "").lower()
    if (u.scheme == "https" and (host == "slack.com" or host.endswith(".slack.com"))) or (
            u.scheme in ("http", "https") and _loopback(host)):
        return url.rstrip("/")
    raise ConfigError(f"[bridges.slack] api must be https on slack.com, or a loopback host for a local fake, not {url!r}: "
                      "the token goes nowhere else")


def slack(raw: dict[str, Any], repo_root: Path) -> SlackConfig:
    s = section(raw, "slack")
    if s is None:
        raise ConfigError("orchestra.toml has no [bridges.slack] section")
    if "token" in s:
        raise ConfigError("[bridges.slack] token: the token never goes in orchestra.toml; set SLACK_USER_TOKEN "
                          "or put it in a file outside the repository (token_file)")
    answered = _strings(s, "answered", ["reaction"])
    if not answered:
        raise ConfigError(f"[bridges.slack] answered must name at least one of {RULES}: with none, nothing answers a ping")
    if bad := [a for a in answered if a not in RULES]:
        raise ConfigError(f"[bridges.slack] answered must be among {RULES}, not {bad!r}")
    groups = _strings(s, "groups", [])
    if bad := [g for g in groups if not GROUP.match(g)]:
        raise ConfigError(f"[bridges.slack] groups are user group ids (S...), not {bad!r}")
    tf = s.get("token_file")
    port = s.get("port", 9465)
    if not isinstance(port, int) or not 0 <= port < 65536:
        raise ConfigError(f"[bridges.slack] port must be a port number, not {port!r}")
    c = SlackConfig(
        every=parse_duration(s.get("every"), 60.0),
        lookback=parse_duration(s.get("lookback"), 3600.0),
        max_catchup=parse_duration(s.get("max_catchup"), 2 * 86400.0),
        overlap=parse_duration(s.get("overlap"), 600.0),
        answered=answered,
        reactions=[r.strip(":") for r in _strings(s, "reactions", ["white_check_mark", "heavy_check_mark"])],
        groups=groups,
        dms=bool(s.get("dms", True)),
        expire=parse_duration(s.get("expire"), 7 * 86400.0),
        host=str(s.get("host") or "127.0.0.1"),
        port=port,
        token_file=Path(tf).expanduser() if isinstance(tf, str) and tf else None,
        api=slack_api(str(s.get("api") or "https://slack.com/api")),
        people=people_file(raw, repo_root),
        queries=_strings(s, "queries", []) or None,
    )
    if c.every < 5:
        raise ConfigError("[bridges.slack] every must be at least 5s")
    return c
