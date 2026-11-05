"""``orchestra bridges scaffold <kind> <name> [--root repo|state|overlay]`` writes a new collector
from a kit (``kits/scaffold.py``); ``orchestra bridges integrations [--json]`` lists the ones
scaffolded and their clearance (``kits/integrations.py``); ``orchestra bridges people [--json]``
proposes identity-map entries from the accounts the relays announced (``people_table.py``; it
writes nothing). ``orchestra --repo PATH bridges ...``
sets ORCHESTRA_REPO; ``python -m orchestra_bridges ...`` is the same. The bridges read no system
themselves: every collector is a script in the owner's repository (or the state directory)."""

from __future__ import annotations

import sys

USAGE = ("usage: orchestra bridges scaffold <kind> <name> [--root repo|state|overlay]\n"
         "       orchestra bridges integrations [--json]\n"
         "       orchestra bridges people [--json]")


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["scaffold"]:
        from .kits.scaffold import main as scaffold
        return scaffold(argv[1:])
    if argv[:1] == ["integrations"]:
        from .kits.integrations import main as integrations
        return integrations(argv[1:])
    if argv[:1] == ["people"]:
        from .people_table import main as people
        return people(argv[1:])
    retired = argv[:1] and (argv[0] in ("github", "github_prs", "slack") or argv[0].startswith("jira"))
    print((f"orchestra bridges {argv[0]}: retired; the bridges read no system themselves: scaffold a "
           f"collector from a kit instead\n" if retired else "") + USAGE, file=sys.stderr)
    return 0 if argv[:1] in (["-h"], ["--help"]) else 2


if __name__ == "__main__":
    sys.exit(main())
