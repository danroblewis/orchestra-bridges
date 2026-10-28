"""The bridges' check of a merged configuration: what ``project.config_edit`` runs as this
plugin's ``check`` (``Callable[[dict], list[str]]``), so a change that would break a bridge is
refused before it is written, not found in the bridge's log after.

It reads every configured section with the bridge's own reader (``config.py``) and adds what the
readers let pass silently: a key no bridge reads (a misspelling is otherwise ignored), a section
under ``[bridges]`` that is no bridge, and two bridges serving ``/metrics`` on one port. It says
nothing about tokens or the network: those are found only when the bridge starts (its log)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from orchestra.config import ConfigError

from . import config as cfg
from .plugin import PORTS, SYSTEMS

POLLING = {"every", "lookback", "max_catchup", "overlap", "host", "port"}
KEYS = {
    "github": POLLING | {"repos", "bots_answer", "api"},
    "github_prs": POLLING | {"repos", "branches", "labels", "label_results", "checks", "api"},
    "jira": POLLING | {"api", "deployment", "projects", "jql", "email", "token_file", "sprint_field", "done_every",
                       "bots_ask"},
    "slack": POLLING | {"answered", "reactions", "groups", "dms", "expire", "token_file", "queries", "api"},
}


def _kind(section: str) -> str:
    return "jira" if cfg.JIRA_SECTION.match(section) else section


def _read(raw: dict, section: str, root: Path) -> None:
    kind = _kind(section)
    if kind == "jira":
        cfg.jira(raw, root, section)
    else:
        getattr(cfg, kind)(raw, root)


def check(raw: dict[str, Any], repo_root: Path | None = None) -> list[str]:
    """What the bridges say against the merged configuration ``raw``; empty when nothing."""
    bridges = raw.get("bridges")
    if bridges is None:
        return []
    if not isinstance(bridges, dict):
        return ["[bridges] must be a table"]
    root = Path(repo_root or ".")
    out: list[str] = []
    people = bridges.get("people")
    if people is not None and not (isinstance(people, str) and people):
        out.append("[bridges] people must be the path of the identity map (a string)")
    ports: dict[int, str] = {}
    for name, s in bridges.items():
        if name == "people":
            continue
        if not isinstance(s, dict) or (name not in SYSTEMS and not cfg.JIRA_SECTION.match(name)):
            out.append(f"[bridges.{name}] is no bridge: the bridges are {', '.join(SYSTEMS)}, jira and jira_<name>")
            continue
        try:
            _read(raw, name, root)
        except (ConfigError, ValueError, TypeError) as e:
            out.append(str(e))
            continue
        if unknown := sorted(set(s) - KEYS[_kind(name)]):
            why = " (a token never goes in orchestra.toml)" if "token" in unknown else ""
            out.append(f"[bridges.{name}] has no setting {', '.join(unknown)}{why}")
        port = s.get("port", PORTS.get(name))
        if isinstance(port, int) and port > 0:
            if port in ports:
                out.append(f"[bridges.{name}] port {port} is [bridges.{ports[port]}]'s too: one of them cannot serve /metrics")
            ports.setdefault(port, name)
    return out
