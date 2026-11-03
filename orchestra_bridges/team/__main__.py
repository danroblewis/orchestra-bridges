"""``python -m orchestra_bridges.team``: the scripted team's working day (``play.py``), or ``--close``.
Exit 0: done; 1: some act failed (printed, and noted in the ledger); 2: refused before any write
(nothing to play against, a credential missing or unsafe, a bad option)."""

from __future__ import annotations

import argparse
import os
import re
import sys

from ..secret import TokenError
from . import settings
from .day import ROUNDS, day
from .ledger import DEFAULT, Ledger
from .play import Team
from .wire import Throttle, WireError


def duration(s: str) -> float:
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(s|m|h)\s*", s or "")
    if not m:
        raise argparse.ArgumentTypeError(f"{s!r}: a duration such as 90s, 20m or 8h")
    return float(m.group(1)) * {"s": 1, "m": 60, "h": 3600}[m.group(2)]


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m orchestra_bridges.team", description=(
        "Play a scripted team's working day in a Jira test project and Discord test channels, so the "
        "read-only pollers have something real to relay. Test tooling: never a machine."))
    p.add_argument("--jira-url", default=os.environ.get("ORCHESTRA_TEAM_JIRA_URL"), help="the Jira site (none: no Jira acts)")
    p.add_argument("--project", default="SCRUM", help="the test project's key (default SCRUM)")
    p.add_argument("--jira-flavour", choices=("auto", "cloud", "datacenter"), default="auto")
    p.add_argument("--jira-token-file", help="default ~/.config/orchestra-bridges/jira-cloud-token")
    p.add_argument("--jira-email-file", help="default ~/.config/orchestra-bridges/jira-cloud-email (Cloud)")
    p.add_argument("--discord-url", default="https://discord.com")
    p.add_argument("--channel", action="append", default=[], help="a Discord test channel's id (repeat)")
    p.add_argument("--channels-from", help="a Discord poller.toml whose [settings] channels to post in (read only)")
    p.add_argument("--discord-token-file", help="default ~/.config/orchestra-bridges/discord-token")
    p.add_argument("--rhythm", type=duration, default=duration("8h"), help="how long the day takes (default 8h; 20m)")
    p.add_argument("--seed", type=int, default=1, help="the day's draw (a new day's; a resumed day keeps its own)")
    p.add_argument("--dry-run", action="store_true", help="print what it would do; write nothing (reads still happen)")
    p.add_argument("--once", action="store_true", help="play one round (the next of an unfinished day) and stop")
    p.add_argument("--close", action="store_true", help="close every ticket the ledger's days created; delete nothing")
    p.add_argument("--resolution", default="Done", help="the resolution --close sets (default Done)")
    p.add_argument("--ledger", default=str(DEFAULT), help=f"the ledger's JSON (default {DEFAULT})")
    return p


def main(argv: list[str] | None = None, clock=None, sleep=None) -> int:
    ns = parser().parse_args(argv)
    try:
        channels = [*ns.channel, *(settings.channels_from(ns.channels_from) if ns.channels_from else [])]
    except (OSError, ValueError) as e:
        print(f"refused: --channels-from: {e}", file=sys.stderr)
        return 2
    ledger = Ledger(ns.ledger)
    throttle = Throttle(**({"clock": clock} if clock else {}), **({"sleep": sleep} if sleep else {}))
    cloud = None if ns.jira_flavour == "auto" else ns.jira_flavour == "cloud"
    try:
        jira = settings.jira(ns.jira_url, throttle, cloud, ns.jira_token_file, ns.jira_email_file) if ns.jira_url else None
        discord = settings.discord(ns.discord_url, throttle, ns.discord_token_file) if channels and not ns.close else None
    except TokenError as e:
        print(f"refused: {e}", file=sys.stderr)
        return 2
    team = Team(ledger, jira, ns.project, discord, channels, ns.dry_run,
                **({"clock": clock} if clock else {}), **({"sleep": sleep} if sleep else {}))
    if ns.close:
        if jira is None:
            print("refused: --close needs --jira-url", file=sys.stderr)
            return 2
        n = team.close(ns.resolution, ns.jira_url)
        print(f"{n} ticket(s) closed or found closed{' (dry run: nothing written)' if ns.dry_run else ''}; "
              f"ledger {ledger.path}")
        return 1 if team.failures else 0
    if jira is None and not channels:
        print("refused: nothing to play against: give --jira-url, or --channel/--channels-from", file=sys.stderr)
        return 2
    project = ns.project if ns.jira_url else None
    d = ledger.current()
    if d is not None and (d.get("jira"), d.get("project"), d.get("channels")) != (ns.jira_url, project, channels):
        d = None                                  # an unfinished day elsewhere is left as it is
    if d is None:
        d = ledger.new_day(ns.seed, ROUNDS, ns.jira_url, project, channels)
        print(f"a new day (seed {ns.seed}), rhythm {ns.rhythm:g}s for {ROUNDS} rounds")
    else:
        print(f"resuming the day started {d['started']} (seed {d['seed']}) at round {d['played'] + 1}")
    try:
        team.look()
    except WireError as e:
        print(f"refused: {e}", file=sys.stderr)
        return 2
    print(f"jira: {ns.jira_url or 'none'} {ns.project if ns.jira_url else ''} people {len(team.people)}; "
          f"discord: {', '.join('#' + team.names.get(c, c) for c in channels) or 'none'}")
    played = team.play(d, day(d["seed"], len(channels)), 1 if ns.once else ROUNDS, ns.rhythm / ROUNDS)
    print(f"{played} round(s) played{' (dry run: nothing written)' if ns.dry_run else ''}; "
          f"{d['played'] if not ns.dry_run else d['played'] + played}/{ROUNDS} of the day; "
          f"{throttle.writes} write(s); ledger {ledger.path if not ns.dry_run else '(untouched)'}")
    return 1 if team.failures else 0


if __name__ == "__main__":
    sys.exit(main())
