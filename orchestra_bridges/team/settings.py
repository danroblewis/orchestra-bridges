"""Where the scripted team plays and as whom: the Jira site and project, the Discord channels, and
the credentials, by file reference only (``secret.read_token``: a ``chmod 600`` file outside this
repository, read once, kept in memory). Against a fake on this machine (a loopback URL) no
credential is read or sent.

- Jira Cloud: HTTP Basic of the account's email and an API token, from
  ``~/.config/orchestra-bridges/jira-cloud-email`` and ``jira-cloud-token`` (``--jira-email-file``,
  ``--jira-token-file``); Data Center (a host not under ``atlassian.net``): the token as ``Bearer``.
- Discord: a bot token from ``~/.config/orchestra-bridges/discord-token`` (``--discord-token-file``).
- The channels: ``--channel ID`` (repeated), or ``--channels-from`` a Discord poller's
  ``poller.toml`` (its ``[settings] channels``), read only."""

from __future__ import annotations

import base64
import tomllib
import urllib.parse
from pathlib import Path

from ..local import is_local
from ..secret import read_token
from .discord import Discord
from .jira import Jira
from .wire import Throttle, Wire

REPO = Path(__file__).resolve().parents[2]          # a token file inside this repository is refused


def is_cloud(url: str) -> bool:
    return (urllib.parse.urlsplit(url).hostname or "").endswith(".atlassian.net")


def channels_from(path: str | Path) -> list[str]:
    s = tomllib.loads(Path(path).expanduser().read_text()).get("settings") or {}
    return [str(c) for c in s.get("channels") or []]


def jira(url: str, throttle: Throttle, cloud: bool | None = None, token_file: str | None = None,
         email_file: str | None = None) -> Jira:
    cloud = is_cloud(url) if cloud is None else cloud
    auth = None
    if not is_local(url):
        token = read_token(None, "jira-cloud-token" if cloud else "jira-token", REPO, token_file)
        if cloud:
            email = read_token(None, "jira-cloud-email", REPO, email_file)
            auth = "Basic " + base64.b64encode(f"{email}:{token}".encode()).decode()
        else:
            auth = f"Bearer {token}"
    return Jira(Wire(url, auth, throttle), cloud)


def discord(url: str, throttle: Throttle, token_file: str | None = None) -> Discord:
    auth = None if is_local(url) else f"Bot {read_token(None, 'discord-token', REPO, token_file)}"
    return Discord(Wire(url, auth, throttle, user_agent="DiscordBot (https://github.com/danroblewis/orchestra, 1)"))
