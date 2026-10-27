"""``python -m orchestra_bridges github|github_prs [--once]``: run a bridge for the project at
ORCHESTRA_REPO (the launcher sets it), else the working directory, until SIGTERM or SIGINT."""

from __future__ import annotations

import argparse
import signal
import sys
import threading

from orchestra.config import ConfigError, load_project
from orchestra.events import Events
from orchestra.metrics import ProjectStore, for_project

from . import config as cfg
from .github import bridge as github
from .github.api import GitHub, GitHubError, gh_token
from .github_prs import bridge as github_prs
from .identity import IdentityError


def _github(project):
    conf = cfg.github(project.config.raw, project.repo_root)
    api = GitHub(gh_token(conf.api), conf.api)
    return github.Bridge(conf, api, Events.for_project(project, source=github.MACHINE),
                         ProjectStore(for_project(project), project.key))


def _github_prs(project):
    conf = cfg.github_prs(project.config.raw, project.repo_root)
    api = GitHub(gh_token(conf.api), conf.api)
    return github_prs.Bridge(conf, api, Events.for_project(project, source=github_prs.MACHINE),
                             ProjectStore(for_project(project), project.key))


BRIDGES = {"github": (_github, github), "github_prs": (_github_prs, github_prs)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="orchestra_bridges")
    ap.add_argument("bridge", choices=sorted(BRIDGES))
    ap.add_argument("--once", action="store_true", help="poll once and exit")
    args = ap.parse_args(argv)
    make, module = BRIDGES[args.bridge]
    stop = threading.Event()
    try:
        bridge = make(load_project())
    except (ConfigError, GitHubError, IdentityError) as e:
        print(f"{module.MACHINE}: {e}", file=sys.stderr)
        return 2
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    try:
        bridge.run(stop, once=args.once)
    except OSError as e:                          # its /metrics port is taken, say
        print(f"{module.MACHINE}: {e}", file=sys.stderr)
        return 2
    module.log("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
