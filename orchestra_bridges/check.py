"""The bridges' check of a merged configuration: what ``project.config_edit`` runs as this
plugin's ``check``, so a change that would break the kits is refused before it is written.

``[bridges]`` has one section, ``[bridges.kits]`` (``kits/settings.py``). The direct readers'
sections (``[bridges.github]``, ``[bridges.github_prs]``, ``[bridges.jira]``, ``[bridges.jira_<name>]``,
``[bridges.slack]``, ``[bridges] people``) were retired on 2026-11-01: each is refused with what
replaced it, a collector scaffolded from a kit (``orchestra bridges scaffold <kit> <name>``)."""

from __future__ import annotations

import re
from typing import Any

from .kits.settings import check as kits_check

RETIRED = re.compile(r"^(github|github_prs|slack|jira(_[a-z0-9]+)*|people)$")
KIT = {"github": "github", "github_prs": "github", "slack": "slack", "people": None}


def check(raw: dict[str, Any], repo_root=None) -> list[str]:
    """What the bridges say against the merged configuration ``raw``; empty when nothing."""
    bridges = raw.get("bridges")
    if bridges is None:
        return []
    if not isinstance(bridges, dict):
        return ["[bridges] must be a table"]
    out: list[str] = []
    for name in bridges:
        if name == "kits":
            out += kits_check(raw)
        elif RETIRED.match(name):
            kit = KIT.get(name, "jira")
            out.append(f"[bridges.{name}] was retired: the bridges read no system themselves; "
                       + (f"scaffold a collector instead (orchestra bridges scaffold {kit} <name>) and run it "
                          "from [[machines]] and [[collect]] entries" if kit else
                          "the identity map is gone: a poller relays each account as its own machine"))
        else:
            out.append(f"[bridges.{name}] is no setting: [bridges] has only [bridges.kits]")
    return out
