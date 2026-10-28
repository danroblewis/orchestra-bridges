"""The bridges' settings: sections of the repository's ``orchestra.toml`` that Orchestra ignores.

::

    [bridges]
    people = "people.toml"            # the identity map (identity.py), relative to the repository

    [bridges.github]
    repos = ["acme/service"]          # owner/name; required
    every = "60s"                     # how often to poll
    lookback = "1h"                   # on the very first start, how far back to read
    max_catchup = "48h"               # after a gap, how far back it will catch up at most
    overlap = "10m"                   # each poll re-reads this much before the last one
    bots_answer = false               # whether a bot's comment or close counts as an issue's first response
    host = "127.0.0.1"                # where /metrics listens: local only unless you change it
    port = 9464                       # where /metrics is served; the plugin scrapes it (0: any port, no scrape)
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

    [bridges.jira]                    # a second instance is [bridges.jira_<name>], e.g. jira_fedramp
    api = "https://jira.acme.com"     # required: the Data Center base URL (or a Cloud site)
    deployment = "datacenter"         # or "cloud"; the default is datacenter, except on *.atlassian.net
    projects = ["ABC", "OPS"]         # follow these projects,
    jql = "project = ABC AND component = api"   # or the issues this JQL matches (one of the two is required)
    every = "60s"
    lookback = "1h"
    max_catchup = "48h"
    overlap = "10m"
    email = "me@acme.com"             # Cloud only: the account the API token belongs to (or JIRA_EMAIL)
    token_file = "~/.config/orchestra-bridges/jira-token"   # never the token itself
    sprint_field = "customfield_10020"   # found from /field when not set
    done_every = "1h"                 # how often done issues are counted (0: never)
    bots_ask = false                  # whether a bot's comment waits for the owner's answer
    host = "127.0.0.1"
    port = 9467                       # /metrics; required for a named instance (0: any port, no scrape)

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
    port = 9465                       # where /metrics is served; the plugin scrapes it (0: any port, no scrape)
    token_file = "~/.config/orchestra-bridges/slack-token"   # never the token itself
    queries = ["<@{me}>", "to:<@{me}>"]   # replaces the default searches (slack/search.py); {me} is your member id
    api = "https://slack.com/api"     # https on slack.com, or a local fake (loopback) in tests

``[bridges] people`` (the identity map) applies to every bridge.

A bridge runs only where its section exists (``plugin.py``). No token is ever in orchestra.toml:
the GitHub bridges use the owner's ``gh`` login (``github/api.py``), the Jira and Slack bridges an
environment variable or a file outside the repository (``secret.py``), and a ``token`` key is refused."""

from __future__ import annotations

import fnmatch
import ipaddress
import re
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from orchestra.config import ConfigError, parse_duration

REPO = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
GITHUB_PORT = 9464


@dataclass
class GitHubConfig:
    repos: list[str]
    every: float = 60.0
    lookback: float = 3600.0
    max_catchup: float = 2 * 86400.0
    overlap: float = 600.0
    bots_answer: bool = False
    host: str = "127.0.0.1"
    port: int = GITHUB_PORT
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
    if "repos" not in s:
        raise ConfigError('[bridges.github] repos is required: the repositories whose issues to follow, as owner/name '
                          '(repos = ["acme/service"])')
    repos = s["repos"]
    if isinstance(repos, str):
        repos = [repos]
    if not isinstance(repos, list):
        raise ConfigError(f"[bridges.github] repos must be a list of owner/name strings, not {repos!r}")
    bad = [r for r in repos if not isinstance(r, str) or not REPO.match(r)]
    if bad or not repos:
        raise ConfigError(f"[bridges.github] repos must be owner/name strings, not {bad or repos!r}")
    port = s.get("port", GITHUB_PORT)
    if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port < 65536:
        raise ConfigError(f"[bridges.github] port must be a port number (0: any free one), not {port!r}")
    c = GitHubConfig(
        repos=list(repos),
        every=parse_duration(s.get("every"), 60.0),
        lookback=parse_duration(s.get("lookback"), 3600.0),
        max_catchup=parse_duration(s.get("max_catchup"), 2 * 86400.0),
        overlap=parse_duration(s.get("overlap"), 600.0),
        bots_answer=bool(s.get("bots_answer", False)),
        host=str(s.get("host") or "127.0.0.1"),
        port=port,
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


def _names(s: dict, name: str, key: str, default: list[str] | None = None) -> list[str]:
    """``[bridges.<name>] key``: a string or a list of non-empty strings (``default`` when absent)."""
    v = s.get(key, default or [])
    if isinstance(v, str):
        v = [v]
    if not isinstance(v, list) or not all(isinstance(x, str) and x for x in v):
        raise ConfigError(f"[bridges.{name}] {key} must be a list of strings, not {v!r}")
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
        branches=_names(s, "github_prs", "branches"),
        labels=_names(s, "github_prs", "labels"),
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


JIRA_SECTION = re.compile(r"^jira(_[a-z0-9]+)*$")
PROJECT_KEY = re.compile(r"^[A-Z][A-Z0-9_]+$")
JIRA_PORT = 9467


def jira_sections(raw: dict[str, Any]) -> list[str]:
    """The Jira bridges a project configures: ``jira``, and any ``jira_<name>`` (a second instance)."""
    return sorted(k for k, v in (raw.get("bridges") or {}).items() if JIRA_SECTION.match(k) and isinstance(v, dict))


@dataclass
class JiraConfig:
    name: str                         # the section: jira, or jira_<instance>
    api: str
    deployment: str                   # cloud or datacenter
    projects: list[str] = field(default_factory=list)
    jql: str | None = None
    every: float = 60.0
    lookback: float = 3600.0
    max_catchup: float = 2 * 86400.0
    overlap: float = 600.0
    email: str | None = None
    token_file: Path | None = None
    sprint_field: str | None = None
    done_every: float = 3600.0
    bots_ask: bool = False
    host: str = "127.0.0.1"
    port: int = JIRA_PORT
    people: Path | None = None

    @property
    def env(self) -> str:
        """The prefix of this instance's environment variables: JIRA, or JIRA_FEDRAMP for jira_fedramp."""
        return self.name.upper()

    @property
    def site(self) -> str:
        """The host (and port) the API is on, as event ids name it."""
        return urllib.parse.urlsplit(self.api).netloc.lower()

    def scope(self) -> str:
        """The JQL clause that picks the issues followed."""
        if self.jql:
            return self.jql
        return "project in (" + ", ".join(f'"{p}"' for p in self.projects) + ")"


def _loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == "localhost"


def jira_api(name: str, url: str) -> str:
    """``api`` when the token may go there: https anywhere (Data Center is the company's own host), or
    plain http only to a fake on this machine."""
    u = urllib.parse.urlsplit(url)
    if u.scheme == "https" and u.hostname or u.scheme == "http" and _loopback((u.hostname or "").lower()):
        return url.rstrip("/")
    raise ConfigError(f"[bridges.{name}] api must be an https URL (or http on a loopback host, for a fake), not {url!r}")


def jira(raw: dict[str, Any], repo_root: Path, name: str = "jira") -> JiraConfig:
    s = section(raw, name)
    if s is None or not JIRA_SECTION.match(name):
        raise ConfigError(f"orchestra.toml has no [bridges.{name}] section")
    where = f"[bridges.{name}]"
    if "token" in s:
        raise ConfigError(f"{where} token: the token never goes in orchestra.toml; set {name.upper()}_API_TOKEN "
                          "or put it in a file outside the repository (token_file)")
    if not isinstance(s.get("api"), str) or not s["api"]:
        raise ConfigError(f"{where} api is required: your Jira site, e.g. https://acme.atlassian.net")
    api = jira_api(name, s["api"])
    host = (urllib.parse.urlsplit(api).hostname or "").lower()
    deployment = s.get("deployment") or ("cloud" if host.endswith(".atlassian.net") else "datacenter")
    if deployment not in ("cloud", "datacenter"):
        raise ConfigError(f"{where} deployment must be cloud or datacenter, not {deployment!r}")
    projects = s.get("projects", [])
    if isinstance(projects, str):
        projects = [projects]
    if not isinstance(projects, list) or not all(isinstance(p, str) and PROJECT_KEY.match(p) for p in projects):
        raise ConfigError(f"{where} projects must be a list of project keys (ABC), not {projects!r}")
    jql = s.get("jql")
    if jql is not None and (not isinstance(jql, str) or not jql.strip()):
        raise ConfigError(f"{where} jql must be a JQL query")
    if jql and re.search(r"\border\s+by\b", jql, re.IGNORECASE):
        raise ConfigError(f"{where} jql must not order: the bridge adds its own ORDER BY")
    if bool(projects) == bool(jql):
        raise ConfigError(f"{where} needs either projects or jql (one of them)")
    port = s.get("port", JIRA_PORT if name == "jira" else None)
    if port is None:
        raise ConfigError(f"{where} port is required for a second instance (another bridge has {JIRA_PORT})")
    if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port < 65536:
        raise ConfigError(f"{where} port must be a port number (0: any free one), not {port!r}")
    tf, email, sprint = s.get("token_file"), s.get("email"), s.get("sprint_field")
    if sprint is not None and not (isinstance(sprint, str) and re.match(r"^customfield_\d+$", sprint)):
        raise ConfigError(f"{where} sprint_field must be a custom field id (customfield_10020), not {sprint!r}")
    c = JiraConfig(
        name=name, api=api, deployment=deployment, projects=list(projects), jql=jql.strip() if jql else None,
        every=parse_duration(s.get("every"), 60.0),
        lookback=parse_duration(s.get("lookback"), 3600.0),
        max_catchup=parse_duration(s.get("max_catchup"), 2 * 86400.0),
        overlap=parse_duration(s.get("overlap"), 600.0),
        email=email if isinstance(email, str) and email else None,
        token_file=Path(tf).expanduser() if isinstance(tf, str) and tf else None,
        sprint_field=sprint,
        done_every=parse_duration(s.get("done_every"), 3600.0),
        bots_ask=bool(s.get("bots_ask", False)),
        host=str(s.get("host") or "127.0.0.1"),
        port=port,
        people=people_file(raw, repo_root),
    )
    if c.every < 5:
        raise ConfigError(f"{where} every must be at least 5s")
    return c


GROUP = re.compile(r"^S[A-Z0-9]+$")
RULES = ("reaction", "reply")


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
    answered = _names(s, "slack", "answered", ["reaction"])
    if not answered:
        raise ConfigError(f"[bridges.slack] answered must name at least one of {RULES}: with none, nothing answers a ping")
    if bad := [a for a in answered if a not in RULES]:
        raise ConfigError(f"[bridges.slack] answered must be among {RULES}, not {bad!r}")
    groups = _names(s, "slack", "groups", [])
    if bad := [g for g in groups if not GROUP.match(g)]:
        raise ConfigError(f"[bridges.slack] groups are user group ids (S...), not {bad!r}")
    tf = s.get("token_file")
    port = s.get("port", 9465)
    if isinstance(port, bool) or not isinstance(port, int) or not 0 <= port < 65536:
        raise ConfigError(f"[bridges.slack] port must be a port number (0: any free one), not {port!r}")
    c = SlackConfig(
        every=parse_duration(s.get("every"), 60.0),
        lookback=parse_duration(s.get("lookback"), 3600.0),
        max_catchup=parse_duration(s.get("max_catchup"), 2 * 86400.0),
        overlap=parse_duration(s.get("overlap"), 600.0),
        answered=answered,
        reactions=[r.strip(":") for r in _names(s, "slack", "reactions", ["white_check_mark", "heavy_check_mark"])],
        groups=groups,
        dms=bool(s.get("dms", True)),
        expire=parse_duration(s.get("expire"), 7 * 86400.0),
        host=str(s.get("host") or "127.0.0.1"),
        port=port,
        token_file=Path(tf).expanduser() if isinstance(tf, str) and tf else None,
        api=slack_api(str(s.get("api") or "https://slack.com/api")),
        people=people_file(raw, repo_root),
        queries=_names(s, "slack", "queries", []) or None,
    )
    if c.every < 5:
        raise ConfigError("[bridges.slack] every must be at least 5s")
    return c
