"""The kits' settings: ``[bridges.kits]`` in ``orchestra.toml`` (or the private overlay), read
through the project's merged configuration, never by opening the file (Orchestra's rule 10).

::

    [bridges.kits]
    cache_dir = "~/.orchestrator/acme-1a2b3c4d/kits/cache"   # default: <state>/kits/cache
    ttl = "5m"                          # how long a cached answer is served (default 300 s)
    per_minute = 60                     # the default request rate per host, for every kit client
    hosts = { "api.pagerduty.com" = 30, "jira.acme.com" = 20 }   # per host, over the default
    tokens = { pagerduty = "PAGERDUTY_TOKEN", jira = "JIRA_API_TOKEN" }   # environment NAMES only

A token is never here: ``tokens`` maps a system to the name of the environment variable that holds
it, and ``check`` refuses a value that is not such a name, and any key named ``token``."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from orchestra.config import ConfigError, parse_duration

ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")
SYSTEM = re.compile(r"^[a-z][a-z0-9_]{0,40}$")
HOST = re.compile(r"^[A-Za-z0-9.-]+(:\d+)?$")
KEYS = {"cache_dir", "ttl", "per_minute", "hosts", "tokens"}
DEFAULT_PER_MINUTE = 60
DEFAULT_TTL_S = 300.0


@dataclass
class KitSettings:
    cache_dir: Path | None = None             # None: <state>/kits/cache
    ttl_s: float = DEFAULT_TTL_S
    per_minute: int = DEFAULT_PER_MINUTE
    hosts: dict[str, int] = field(default_factory=dict)
    tokens: dict[str, str] = field(default_factory=dict)

    def per_minute_for(self, host: str) -> int:
        return self.hosts.get(host.lower(), self.per_minute)

    def token_env(self, system: str, default: str | None = None) -> str | None:
        return self.tokens.get(system, default)


def _rate(v: Any, where: str) -> int:
    if isinstance(v, bool) or not isinstance(v, int) or not 0 < v <= 6000:
        raise ConfigError(f"{where} must be a number of requests a minute (1 to 6000), not {v!r}")
    return v


def read(raw: dict[str, Any]) -> KitSettings:
    """``[bridges.kits]`` of a merged configuration (defaults when absent). Raises ConfigError."""
    s = (raw.get("bridges") or {}).get("kits")
    if s is None:
        return KitSettings()
    if not isinstance(s, dict):
        raise ConfigError("[bridges.kits] must be a table")
    if "token" in s:
        raise ConfigError("[bridges.kits] token: a token never goes in orchestra.toml; name its environment "
                          "variable in tokens instead (tokens = { pagerduty = \"PAGERDUTY_TOKEN\" })")
    if unknown := sorted(set(s) - KEYS):
        raise ConfigError(f"[bridges.kits] has no setting {', '.join(unknown)}")
    cache = s.get("cache_dir")
    if cache is not None and not (isinstance(cache, str) and cache):
        raise ConfigError(f"[bridges.kits] cache_dir must be a path, not {cache!r}")
    ttl = parse_duration(s.get("ttl"), DEFAULT_TTL_S)
    if ttl < 0:
        raise ConfigError("[bridges.kits] ttl must not be negative")
    hosts = s.get("hosts", {})
    if not isinstance(hosts, dict) or not all(isinstance(h, str) and HOST.match(h) for h in hosts):
        raise ConfigError("[bridges.kits] hosts maps a host name to its requests a minute")
    tokens = s.get("tokens", {})
    if not isinstance(tokens, dict):
        raise ConfigError("[bridges.kits] tokens maps a system to an environment variable's name")
    for k, v in tokens.items():
        if not SYSTEM.match(str(k)):
            raise ConfigError(f"[bridges.kits] tokens: {k!r} is not a system name (lower case, digits, _)")
        if not isinstance(v, str) or not ENV_NAME.match(v):
            # never echo the value: it may be the token itself, pasted by mistake
            raise ConfigError(f"[bridges.kits] tokens.{k} must be the NAME of an environment variable "
                              "(upper case, digits, _), never the token")
    return KitSettings(
        cache_dir=Path(cache).expanduser() if cache else None, ttl_s=ttl,
        per_minute=_rate(s.get("per_minute", DEFAULT_PER_MINUTE), "[bridges.kits] per_minute"),
        hosts={h.lower(): _rate(v, f"[bridges.kits] hosts.{h}") for h, v in hosts.items()},
        tokens=dict(tokens))


def check(raw: dict[str, Any]) -> list[str]:
    """What is wrong with ``[bridges.kits]``, for the plugin's ``check``."""
    try:
        read(raw)
    except (ConfigError, ValueError, TypeError) as e:
        return [str(e)]
    return []


def cache_dir(settings: KitSettings, state_dir: Path) -> Path:
    return settings.cache_dir or state_dir / "kits" / "cache"
