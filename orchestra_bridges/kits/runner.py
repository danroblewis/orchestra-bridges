"""The command line every scaffolded poller shares, so its own file holds only what it reads and
what it publishes:

- ``python <name>.py [--once | --rounds N] [--every S] [--base-url URL] [--state-dir DIR]
  [--events-file FILE] [--ttl S] [--dry-run] [--set KEY=VALUE ...]``: the poller, relaying events
  (``Poller``);
- ``python <name>.py measure [--base-url URL] [--state-dir DIR] [--ttl S]``: one measurement,
  printed as Prometheus text on standard output, for the ``[[collect]]`` entry that stores it (an
  instrument: it measures and never publishes).

Without ``--state-dir`` and ``--events-file`` it runs in the project at ORCHESTRA_REPO (the launcher
sets it; else the working directory): events on the project's bus, the cursor under
``<state>/kits/pollers/<name>/`` and the cache under ``[bridges.kits] cache_dir``
(``<state>/kits/cache``). A test gives both, and a fake's URL. Before anything is read, the
compliance check (``machinefile.require_clearance``).

**Waiting for approval.** The poller that runs for good (no ``--once``, no ``--rounds``) and is not
cleared to read its system does not exit, so a ``[[machines]]`` entry written before the owner's
approval does not crash-loop under ``restart = "always"``: it logs once ``waiting for approval``,
reads nothing, and re-reads ``machine.toml`` on its interval until ``clearance = "approved"`` (then
it polls) or it is stopped (SIGTERM, SIGINT: exit 0). Against a fake on this machine it is cleared
and polls at once. ``--once``, ``--rounds`` and ``measure`` still refuse (exit 2) before any read.

The token comes from ``[settings] token_env`` (an environment variable's name) or ``token_file`` (a
``chmod 600`` file outside any repository). **A fake never gets a credential**: against a loopback
URL the runner reads neither the token nor the account and sends placeholders instead,
``[settings] fake_token`` and ``fake_user`` when set, else ``FAKE_TOKEN`` and ``FAKE_USER`` (a test
starts its fake with the same, ``testing.fake_token``), so a real token is sent only to the real
host. Anywhere else a missing token is an error, as ever. Against a fake the poller's notes say so
(``notes.FAKE``, on each actor's announcement), beside ``machine.toml``'s ``[notes]``. ``ca_file``
is passed to the client (a corporate CA); ``HTTPS_PROXY``/``NO_PROXY`` are the client's. ``measure``
finds the poller's directory in ``MachineFile.poll_dir``, so it can print the counters the poller
keeps (``pollstate.counters_at``) beside what it reads itself.

A measurement-only program (a metrics store has no item events to relay) passes no ``make_poll``;
running it without ``measure`` refuses, exit 2.

**Exit codes**, the same for every scaffolded program: 0 done (a poll run that ended, a stop while
waiting, a measurement printed); 1 a measurement that failed (its read raised); 2 refused before any
read (a ``machine.toml`` that cannot be read, no project, not cleared for ``--once``/``--rounds``/
``measure``, a missing token, a poll asked of a measurement-only program)."""

from __future__ import annotations

import argparse
import signal
import sys
import threading
import time
import tomllib
import urllib.parse
from collections.abc import Callable
from pathlib import Path

from orchestra.events import Events

from . import notes as note
from .client import CachedClient, KitError, read_token
from .exposition import Registry
from .machinefile import MachineFile, MachineFileError, is_local, read, require_clearance
from .poller import Poller, poll_dir

FAKE_TOKEN = "fake-token-for-a-local-fake"     # what a run against a fake sends instead of a token
FAKE_USER = "fake-user"                         # and instead of an HTTP Basic account
FAKE_RATE = 6000                                # requests a minute to a fake: no real system to spare
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
    ap.add_argument("--set", action="append", default=[], metavar="KEY=VALUE",
                    help="this run only: one of the kit's own [settings] (a TOML value, jql='\"project = ABC\"'), "
                         "as a test sets the scope its fake holds")
    return ap


FIXED = {"base_url", "token", "token_env", "token_file", "user_env", "user_file", "every", "per_minute", "ttl",
         "ca_file", "fake_token", "fake_user"}


def overrides(pairs: list[str]) -> dict:
    """``--set`` pairs as settings; never the URL or the credentials (those have their own paths)."""
    out = {}
    for pair in pairs:
        key, eq, value = pair.partition("=")
        key = key.strip()
        if not eq or not key:
            raise ValueError(f"--set {key or pair!r}: KEY=VALUE, the value in TOML")
        if key in FIXED:
            raise ValueError(f"--set {key}: not settable here (--base-url, --every, --ttl, or machine.toml)")
        try:
            out[key] = tomllib.loads(f"v = {value}")["v"]
        except tomllib.TOMLDecodeError:
            raise ValueError(f"--set {key}: the value is not TOML (a string is quoted)") from None
    return out


def cleared(m: MachineFile, base_url: str) -> bool:
    return is_local(base_url) or m.clearance == "approved"


def credentials(m: MachineFile, base_url: str, repo_root: Path | None = None) -> dict:
    """What the client is given to authenticate: against a fake on this machine the placeholders
    (never a credential read from anywhere), else the token's and the account's references (a file
    inside ``repo_root`` is refused)."""
    if is_local(base_url):
        return {"token": m.fake_token or FAKE_TOKEN, "basic_user": m.fake_user or FAKE_USER}
    user = read_token(m.user_env, m.user_file, repo_root) if m.user_env or m.user_file else None
    return {"token_env": m.token_env, "token_file": m.token_file, "basic_user": user, "repo_root": repo_root}


def _log(name: str, msg: str) -> None:
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {name}: {msg}", file=sys.stderr, flush=True)


def wait_for_clearance(path: Path, m: MachineFile, base_url: str | None, every_s: float,
                       stop: threading.Event) -> MachineFile | None:
    """Sleep on ``every_s`` until ``path`` says ``clearance = "approved"`` (return it, read again) or
    ``stop`` is set (return None). Says so once; reads nothing but the file."""
    host = urllib.parse.urlsplit(base_url or m.base_url).hostname
    _log(m.name, f"waiting for approval: reading {host} is not cleared (machine.toml clearance = {m.clearance!r}); "
                 f"reading nothing, checking machine.toml every {every_s:g}s")
    while not stop.wait(every_s):
        try:
            m = read(path)
        except MachineFileError:
            continue                    # being edited: look again next time
        if cleared(m, base_url or m.base_url):
            _log(m.name, "approved: polling")
            return m
    return None


def _until_stopped() -> threading.Event:
    stop = threading.Event()
    if threading.current_thread() is threading.main_thread():
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *_: stop.set())
    return stop


def main(here: str | Path, make_poll: Callable[[CachedClient, MachineFile], Poll] | None = None,
         argv: list[str] | None = None, *,
         measure: Measure | None = None, auth_format: str | Callable[[MachineFile], str] = "Bearer {token}",
         headers: dict[str, str] | None = None) -> int:
    here = Path(here)
    try:
        m = read(here / "machine.toml")
    except MachineFileError as e:
        print(e, file=sys.stderr)
        return 2
    a = parser(m.name).parse_args(argv)
    if a.what == "poll" and make_poll is None:
        print(f"{m.name}: polls nothing (a measurement only): run it with measure", file=sys.stderr)
        return 2
    if a.what == "poll" and not a.once and a.rounds is None and not cleared(m, a.base_url or m.base_url):
        m = wait_for_clearance(here / "machine.toml", m, a.base_url, a.every or m.every_s, _until_stopped())
        if m is None:
            return 0
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
        m.extra.update(overrides(a.set))
        m.poll_dir = poll_dir(m.name, a.state_dir, project)
        common = {**credentials(m, base_url, project.repo_root if project is not None else None), "auth_format": auth_format(m) if callable(auth_format) else auth_format,
                  "headers": headers, "dry_run": a.dry_run, "ca_file": m.ca_file}
        rate = FAKE_RATE if is_local(base_url) else m.per_minute     # a fake on this machine has no rate to keep
        if a.state_dir:
            client = CachedClient(base_url, per_minute=rate, cache_dir=Path(a.state_dir) / "cache",
                                  ttl_s=m.ttl_s, **common)
        else:
            client = CachedClient.for_project(project, base_url, system=m.kit, per_minute=rate, ttl_s=m.ttl_s,
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
    notes = {**m.notes, **({"fake": note.FAKE} if is_local(base_url) else {})}
    poller = Poller(m.name, every_s=a.every or m.every_s, state_dir=a.state_dir, events=events, project=project,
                    notes=notes)
    return poller.run(make_poll(client, m), once=a.once, rounds=a.rounds)
