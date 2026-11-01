"""The command line every scaffolded poller shares, so its own file holds only what it reads and
what it publishes:

- ``python <name>.py [--once | --rounds N] [--every S] [--base-url URL] [--state-dir DIR]
  [--events-file FILE] [--ttl S] [--dry-run]``: the poller, relaying events (``Poller``);
- ``python <name>.py measure [--base-url URL] [--state-dir DIR] [--ttl S]``: one measurement,
  printed as Prometheus text on standard output, for the ``[[collect]]`` entry that stores it (an
  instrument: it measures and never publishes).

Without ``--state-dir`` and ``--events-file`` it runs in the project at ORCHESTRA_REPO (the launcher
sets it; else the working directory): events on the project's bus, the cursor under
``<state>/kits/pollers/<name>/`` and the cache under ``[bridges.kits] cache_dir``
(``<state>/kits/cache``). A test gives both, and a fake's URL. Before anything is read, the
compliance check (``machinefile.require_clearance``).

The token comes from ``[settings] token_env`` (an environment variable's name) or ``token_file`` (a
``chmod 600`` file outside any repository). A fake on this machine needs neither: against a
loopback URL with the variable unset and no such file, no token is sent (anywhere else a missing
token is an error, as ever). ``measure`` finds the poller's directory in ``MachineFile.poll_dir``, so it can print the counters the poller keeps
(``pollstate.counters_at``) beside what it reads itself."""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Callable
from pathlib import Path

from orchestra.events import Events

from .client import CachedClient, KitError
from .exposition import Registry
from .machinefile import MachineFile, MachineFileError, is_local, read, require_clearance
from .poller import Poller, poll_dir

Poll = Callable[[Poller], None]
Measure = Callable[[CachedClient, MachineFile, Registry], None]


def parser(name: str) -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog=name)
    ap.add_argument("what", nargs="?", choices=["poll", "measure"], default="poll",
                    help="poll (the default: relay events on an interval) or measure (print series once)")
    ap.add_argument("--once", action="store_true", help="poll once and exit")
    ap.add_argument("--rounds", type=int, help="poll this many times and exit")
    ap.add_argument("--every", type=float, help="seconds between polls (machine.toml's every otherwise)")
    ap.add_argument("--base-url", help="the API to read (a fake's, in a test)")
    ap.add_argument("--state-dir", help="where the cursor and the cache go (the project's state directory otherwise)")
    ap.add_argument("--events-file", help="publish to this JSONL file instead of the project's bus")
    ap.add_argument("--ttl", type=float, help="seconds a cached answer is served (machine.toml's ttl otherwise)")
    ap.add_argument("--dry-run", action="store_true", help="answer only from the cache; send nothing")
    return ap


def main(here: str | Path, make_poll: Callable[[CachedClient, MachineFile], Poll], argv: list[str] | None = None, *,
         measure: Measure | None = None, auth_format: str = "Bearer {token}",
         headers: dict[str, str] | None = None) -> int:
    here = Path(here)
    try:
        m = read(here / "machine.toml")
    except MachineFileError as e:
        print(e, file=sys.stderr)
        return 2
    a = parser(m.name).parse_args(argv)
    base_url = a.base_url or m.base_url
    project = None
    if not a.state_dir or (a.what == "poll" and not a.events_file):
        from orchestra.config import ConfigError, load_project
        try:
            project = load_project()
        except ConfigError as e:
            print(f"{m.name}: {e}", file=sys.stderr)
            return 2
    try:
        require_clearance(m, base_url)
        if a.ttl is not None:
            m.ttl_s = a.ttl
        token_env, token_file = m.token_env, m.token_file
        if is_local(base_url) and not (token_env and os.environ.get(token_env)) and not (
                token_file and Path(token_file).expanduser().is_file()):
            token_env = token_file = None       # a fake on this machine: no credentials needed
        m.poll_dir = poll_dir(m.name, a.state_dir, project)
        common = {"token_env": token_env, "token_file": token_file, "auth_format": auth_format, "headers": headers, "dry_run": a.dry_run}
        if a.state_dir:
            client = CachedClient(base_url, per_minute=m.per_minute, cache_dir=Path(a.state_dir) / "cache",
                                  ttl_s=m.ttl_s, **common)
        else:
            client = CachedClient.for_project(project, base_url, system=m.kit, per_minute=m.per_minute, ttl_s=m.ttl_s,
                                              **common)
    except (MachineFileError, KitError, ValueError) as e:
        print(f"{m.name}: {e}", file=sys.stderr)
        return 2
    if a.what == "measure":
        if measure is None:
            print(f"{m.name}: measures nothing", file=sys.stderr)
            return 2
        series = Registry()
        try:
            measure(client, m, series)
        except Exception as e:  # noqa: BLE001 - say what failed; never a body or a header
            print(f"{m.name}: measure failed: {type(e).__name__}: {e}", file=sys.stderr)
            return 1
        series.write("-")
        return 0
    events = Events(Path(a.events_file)) if a.events_file else None
    poller = Poller(m.name, every_s=a.every or m.every_s, state_dir=a.state_dir, events=events, project=project)
    return poller.run(make_poll(client, m), once=a.once, rounds=a.rounds)
