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

A bridge runs only where its section exists (``plugin.py``)."""

from __future__ import annotations

import re
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
