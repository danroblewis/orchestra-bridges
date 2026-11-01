"""The bridges' check of a merged configuration: what ``project.config_edit`` runs as this
plugin's ``check``, so a change that would break the kits is refused before it is written.

``[bridges]`` holds ``[bridges.kits]`` (``kits/settings.py``) and ``people``, the identity map's
path (``identity.py``: a pollers' account it lists is that person, whichever system they acted in).
The direct readers' sections (``[bridges.github]``, ``[bridges.github_prs]``, ``[bridges.jira]``,
``[bridges.jira_<name>]``, ``[bridges.slack]``) were retired on 2026-11-01: each is refused with
what replaced it, a collector scaffolded from a kit (``orchestra bridges scaffold <kit> <name>``)."""

from __future__ import annotations

import re
from typing import Any

from .kits.settings import check as kits_check

RETIRED = re.compile(r"^(github|github_prs|slack|jira(_[a-z0-9]+)*)$")
KIT = {"github": "github", "github_prs": "github", "slack": "slack"}


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
        elif name == "people":
            if not (isinstance(bridges[name], str) and bridges[name]):
                out.append("[bridges] people must be the path of the identity map (a string)")
        elif RETIRED.match(name):
            kit = KIT.get(name, "jira")
            out.append(f"[bridges.{name}] was retired: the bridges read no system themselves; scaffold a collector "
                       f"instead (orchestra bridges scaffold {kit} <name>) and run it from [[machines]] and "
                       "[[collect]] entries")
        else:
            out.append(f"[bridges.{name}] is no setting: [bridges] has only [bridges.kits] and people")
    return out
