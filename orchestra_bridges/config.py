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

    [bridges.github_prs]
    repos = ["acme/service"]          # owner/name; required
    branches = ["agent/*"]            # follow pull requests whose branch matches one of these globs
    labels = ["SAIL"]                 # or that carry one of these labels; with neither, every pull request
    every = "60s"
    lookback = "1h"
    max_catchup = "48h"
    overlap = "10m"
    checks = true                     # read the head's check runs and commit statuses each round
    host = "127.0.0.1"                # where /metrics listens: local only unless you change it
    port = 9466                       # where /metrics is served; the plugin scrapes it (0: any port, no scrape)
    api = "https://api.github.com"

    [bridges.github_prs.label_results]
    SAIL = ["SAIL-passed", "SAIL-failed"]   # a pull request carrying SAIL and neither result awaits one

``[bridges] people`` (the identity map) applies to every bridge.

A bridge runs only where its section exists (``plugin.py``). No token is ever in orchestra.toml:
the GitHub bridges use the owner's ``gh`` login (``github/api.py``), and a ``token`` key is refused."""

from __future__ import annotations

import fnmatch
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


@dataclass
class GitHubPRsConfig:
    repos: list[str]
    branches: list[str] = field(default_factory=list)
    labels: list[str] = field(default_factory=list)
    label_results: dict[str, list[str]] = field(default_factory=dict)
    every: float = 60.0
    lookback: float = 3600.0
    max_catchup: float = 2 * 86400.0
    overlap: float = 600.0
    checks: bool = True
    host: str = "127.0.0.1"
    port: int = 9466
    api: str = "https://api.github.com"
    people: Path | None = None

    def follows(self, branch: str, labels: list[str]) -> bool:
        """Whether a pull request on ``branch`` carrying ``labels`` is one to follow."""
        if not self.branches and not self.labels:
            return True
        return any(fnmatch.fnmatchcase(branch, g) for g in self.branches) or bool(set(labels) & set(self.labels))


def _names(s: dict, key: str) -> list[str]:
    v = s.get(key, [])
    if isinstance(v, str):
        v = [v]
    if not isinstance(v, list) or not all(isinstance(x, str) and x for x in v):
        raise ConfigError(f"[bridges.github_prs] {key} must be a list of strings, not {v!r}")
    return list(v)


def github_prs(raw: dict[str, Any], repo_root: Path) -> GitHubPRsConfig:
    s = section(raw, "github_prs")
    if s is None:
        raise ConfigError("orchestra.toml has no [bridges.github_prs] section")
    if "token" in s:
        raise ConfigError("[bridges.github_prs] token: the token never goes in orchestra.toml; "
                          "the bridge uses your gh login (or GH_TOKEN)")
    repos = s.get("repos")
    if isinstance(repos, str):
        repos = [repos]
    if not isinstance(repos, list) or not repos or not all(isinstance(r, str) and REPO.match(r) for r in repos):
        raise ConfigError(f"[bridges.github_prs] repos must be a list of owner/name strings, not {repos!r}")
    results = s.get("label_results", {})
    if not isinstance(results, dict) or not all(
            isinstance(v, list) and v and all(isinstance(x, str) and x for x in v) for v in results.values()):
        raise ConfigError("[bridges.github_prs.label_results] maps a label to the list of labels that report its result")
    port = s.get("port", 9466)
    if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port < 65536:
        raise ConfigError(f"[bridges.github_prs] port must be a port number (0: any free one), not {port!r}")
    c = GitHubPRsConfig(
        repos=list(repos),
        branches=_names(s, "branches"),
        labels=_names(s, "labels"),
        label_results={k: list(v) for k, v in results.items()},
        every=parse_duration(s.get("every"), 60.0),
        lookback=parse_duration(s.get("lookback"), 3600.0),
        max_catchup=parse_duration(s.get("max_catchup"), 2 * 86400.0),
        overlap=parse_duration(s.get("overlap"), 600.0),
        checks=bool(s.get("checks", True)),
        host=str(s.get("host") or "127.0.0.1"),
        port=port,
        api=str(s.get("api") or "https://api.github.com").rstrip("/"),
        people=people_file(raw, repo_root),
    )
    if c.every < 5:
        raise ConfigError("[bridges.github_prs] every must be at least 5s")
    return c
