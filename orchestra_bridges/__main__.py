"""``python -m orchestra_bridges github|slack [--once]``: run a bridge for the project at ORCHESTRA_REPO
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
from .github import bridge as github
from .github.api import GitHub, GitHubError, gh_token
from .identity import IdentityError
from .slack import bridge as slack
from .slack.api import Slack
from .slack.token import TokenError, user_token


def _github(project, stop: threading.Event):
    conf = cfg.github(project.config.raw, project.repo_root)
    api = GitHub(gh_token(conf.api), conf.api)
    return github.Bridge(conf, api, Events.for_project(project, source=github.MACHINE),
                         ProjectStore(for_project(project), project.key))


def _slack(project, stop: threading.Event):
    """A Retry-After is waited out on ``stop`` (so SIGTERM ends it) and for no longer than a poll."""
    conf = cfg.slack(project.config.raw, project.repo_root)
    api = Slack(user_token(project.repo_root, conf.token_file), conf.api, sleep=stop.wait, max_wait=conf.every)
    return slack.Bridge(conf, api, Events.for_project(project, source=slack.MACHINE),
                        ProjectStore(for_project(project), project.key))


BRIDGES = {"github": (_github, github), "slack": (_slack, slack)}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="orchestra_bridges")
    ap.add_argument("bridge", choices=sorted(BRIDGES))
    ap.add_argument("--once", action="store_true", help="poll once and exit")
    args = ap.parse_args(argv)
    make, module = BRIDGES[args.bridge]
    stop = threading.Event()
    try:
        bridge = make(load_project(), stop)
    except (ConfigError, GitHubError, IdentityError, TokenError) as e:
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
