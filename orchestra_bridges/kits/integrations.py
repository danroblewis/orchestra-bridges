"""``orchestra bridges integrations [--json]``: read-only, the kits there are and the machines
scaffolded from them under the project's roots (``scaffold.ROOTS``: the state directory, beside the
private overlay, the repository's ``.orchestra/``), each with its kit, clearance and data class, the
NAME of its token's variable (never a value; ``token_file`` is shown as ``file``), what schedules
its poller (``[[machines]]``; ``every``: a Workflows ``workflows.every.<item>`` whose role is the
poller's name, with the ``machine.json`` the floor runs it from beside its ``poller.toml``; or
``no``), whether its ``[[collect]]`` entry is in the configuration, and how many values in its
``poller.toml`` are still marked ``# guess``. It changes nothing."""

from __future__ import annotations

import argparse
import json
import sys
import tomllib
from dataclasses import asdict, dataclass
from pathlib import Path

from .machinefile import LEGACY, MachineFileError, locate, read
from .scaffold import KINDS, ROOTS, root_dir
from .settings import read as read_settings


@dataclass
class Integration:
    name: str
    kit: str
    root: str
    path: str
    clearance: str
    data_class: str
    token: str                          # $NAME, "file", or "none"
    schedule: str | None                # "[[machines]]", "every" (Workflows) or "no"; None: the kit has no poller
    collect_entry: bool
    guesses: int
    error: str | None = None


def _names(raw: dict, table: str) -> set[str]:
    return {str(e.get("name")) for e in raw.get(table) or [] if isinstance(e, dict)}


def every_roles(raw: dict) -> set[str]:
    """The roles of the Workflows plugin's clock items (``[workflows.every.<item>]``, ``role``)."""
    every = (raw.get("workflows") or {}).get("every") if isinstance(raw.get("workflows"), dict) else None
    return {str(v.get("role")) for v in (every or {}).values() if isinstance(v, dict) and v.get("role")} \
        if isinstance(every, dict) else set()


def schedule(name: str, folder: Path, machines: set[str], roles: set[str]) -> str:
    if name in machines:
        return "[[machines]]"
    return "every" if name in roles and (folder / "machine.json").is_file() else "no"


def scaffolded(path: Path) -> bool:
    try:
        raw = tomllib.loads(path.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return True                     # a broken one is listed with its error
    return raw.get("kit") in KINDS or raw.get("created_by") == "orchestra bridges scaffold"


def listing(project) -> list[Integration]:
    raw = project.config.raw
    try:
        tokens = read_settings(raw).tokens
    except ValueError:
        tokens = {}
    machines, collect, roles = _names(raw, "machines"), _names(raw, "collect"), every_roles(raw)
    out, seen = [], set()
    for root in ROOTS:
        base = root_dir(project, root) / "machines"
        if base.resolve() in seen:
            continue                    # the overlay beside the state directory: one root, listed once
        seen.add(base.resolve())
        for f in sorted(locate(d) for d in base.iterdir() if d.is_dir()) if base.is_dir() else []:
            if not f.exists() or (f.name == LEGACY and not scaffolded(f)):
                continue                # another machine's files (orchestra-agents keeps its specs here)
            text = f.read_text(encoding="utf-8", errors="replace")
            guesses = sum("# guess" in line for line in text.splitlines())
            try:
                m = read(f)
            except MachineFileError as e:
                out.append(Integration(f.parent.name, "?", root, str(f.parent), "?", "?", "?", None, False, guesses,
                                       str(e)))
                continue
            env = m.token_env or tokens.get(m.kit)
            token = f"${env}" if env else "file" if m.token_file else "none"
            poller = KINDS[m.kit].poller if m.kit in KINDS else True
            out.append(Integration(m.name, m.kit or "?", root, str(f.parent), m.clearance, m.data_class, token,
                                   schedule(m.name, f.parent, machines, roles) if poller else None, m.name in collect,
                                   guesses))
    return out


def _yes(v: bool | None) -> str:
    return "-" if v is None else "yes" if v else "no"


def render(items: list[Integration]) -> str:
    lines = [f"kits: {', '.join(sorted(KINDS))}  (orchestra bridges scaffold <kit> <name>)"]
    if not items:
        lines.append("no machines scaffolded from a kit under the state directory, the overlay or .orchestra/")
        return "\n".join(lines)
    head = ("name", "kit", "root", "clearance", "data_class", "token", "scheduled", "[[collect]]", "guesses")
    rows = [head] + [(i.name, i.kit, i.root, i.clearance, i.data_class, i.token, i.schedule or "-",
                      _yes(i.collect_entry), str(i.guesses)) for i in items]
    widths = [max(len(r[c]) for r in rows) for c in range(len(head))]
    lines += ["  ".join(v.ljust(w) for v, w in zip(r, widths, strict=True)).rstrip() for r in rows]
    lines += [f"{i.name}: {i.error}" for i in items if i.error]
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="orchestra bridges integrations",
                                 description="List the kits and the machines scaffolded from them (read-only).")
    ap.add_argument("--json", action="store_true", help="one JSON object per machine, for a tool")
    a = ap.parse_args(argv)
    from orchestra.config import ConfigError, load_project
    try:
        items = listing(load_project())
    except (ConfigError, OSError) as e:
        print(f"integrations: {e}", file=sys.stderr)
        return 2
    if a.json:
        print(json.dumps({"kits": sorted(KINDS), "machines": [asdict(i) for i in items]}, indent=2))
    else:
        print(render(items))
    return 0


if __name__ == "__main__":
    sys.exit(main())
