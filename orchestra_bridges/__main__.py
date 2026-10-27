"""``python -m orchestra_bridges github [--once]``: run a bridge for the project at ORCHESTRA_REPO
(the launcher sets it), else the working directory, until SIGTERM or SIGINT."""

from __future__ import annotations

import argparse
import signal
import sys
import threading

from orchestra.config import ConfigError, load_project
from orchestra.events import Events
from orchestra.metrics import ProjectStore, for_project

from . import config as cfg
from .github.api import GitHub, GitHubError, gh_token
from .github.bridge import MACHINE, Bridge, log
from .identity import IdentityError


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="orchestra_bridges")
    ap.add_argument("bridge", choices=["github"])
    ap.add_argument("--once", action="store_true", help="poll once and exit")
    args = ap.parse_args(argv)
    try:
        project = load_project()
        conf = cfg.github(project.config.raw, project.repo_root)
        api = GitHub(gh_token(conf.api), conf.api)
        bridge = Bridge(conf, api, Events.for_project(project, source=MACHINE),
                        ProjectStore(for_project(project), project.key))
    except (ConfigError, GitHubError, IdentityError) as e:
        print(f"{MACHINE}: {e}", file=sys.stderr)
        return 2
    stop = threading.Event()
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    bridge.run(stop, once=args.once)
    log("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
