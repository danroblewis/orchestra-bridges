"""``python -m orchestra_bridges github|github_prs|slack|jira[_<name>] [--once]``: run a bridge for the
project at ORCHESTRA_REPO (the launcher sets it), else the working directory, until SIGTERM or
SIGINT."""

from __future__ import annotations

import argparse
import os
import signal
import sys
import threading
import time

from orchestra.config import ConfigError, load_project
from orchestra.events import Events
from orchestra.metrics import ProjectStore, for_project

from . import config as cfg
from .github import bridge as github
from .github.api import GitHub, GitHubError, gh_token
from .github_prs import bridge as github_prs
from .identity import IdentityError
from .jira import bridge as jira
from .jira.api import Jira, JiraError
from .secret import TokenError, read_token
from .slack import bridge as slack
from .slack.api import Slack


def _github(project, stop: threading.Event):
    conf = cfg.github(project.config.raw, project.repo_root)
    api = GitHub(gh_token(conf.api), conf.api)
    return github.Bridge(conf, api, Events.for_project(project, source=github.MACHINE),
                         ProjectStore(for_project(project), project.key))


def _github_prs(project, stop: threading.Event):
    conf = cfg.github_prs(project.config.raw, project.repo_root)
    api = GitHub(gh_token(conf.api), conf.api)
    return github_prs.Bridge(conf, api, Events.for_project(project, source=github_prs.MACHINE),
                             ProjectStore(for_project(project), project.key))


def _jira(project, name: str, stop: threading.Event):
    conf = cfg.jira(project.config.raw, project.repo_root, name)
    token = read_token(f"{conf.env}_API_TOKEN", f"{conf.name}-token", project.repo_root, conf.token_file)
    email = os.environ.get(f"{conf.env}_EMAIL") or conf.email
    api = Jira(conf.api, token, deployment=conf.deployment, email=email, sleep=stop.wait)
    machine = f"bridges.{name}"
    return jira.Bridge(conf, api, Events.for_project(project, source=machine),
                       ProjectStore(for_project(project), project.key))


def _slack(project, stop: threading.Event):
    """A Retry-After is waited out on ``stop`` (so SIGTERM ends it) and for no longer than a poll."""
    conf = cfg.slack(project.config.raw, project.repo_root)
    token = read_token("SLACK_USER_TOKEN", "slack-token", project.repo_root, conf.token_file)
    api = Slack(token, conf.api, sleep=stop.wait, max_wait=conf.every)
    return slack.Bridge(conf, api, Events.for_project(project, source=slack.MACHINE),
                        ProjectStore(for_project(project), project.key))


BRIDGES = {"github": (_github, github), "github_prs": (_github_prs, github_prs), "slack": (_slack, slack)}


def _log(name: str, msg: str) -> None:
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} bridges.{name}: {msg}", file=sys.stderr, flush=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="orchestra_bridges")
    ap.add_argument("bridge", help=f"{', '.join(sorted(BRIDGES))}, jira, or jira_<name>")
    ap.add_argument("--once", action="store_true", help="poll once and exit")
    args = ap.parse_args(argv)
    if args.bridge not in BRIDGES and not cfg.JIRA_SECTION.match(args.bridge):
        ap.error(f"no bridge {args.bridge!r}")
    stop = threading.Event()
    try:
        if args.bridge in BRIDGES:
            bridge = BRIDGES[args.bridge][0](load_project(), stop)
        else:
            bridge = _jira(load_project(), args.bridge, stop)
    except (ConfigError, GitHubError, IdentityError, JiraError, TokenError) as e:
        _log(args.bridge, str(e))
        return 2
    for sig in (signal.SIGTERM, signal.SIGINT):
        signal.signal(sig, lambda *_: stop.set())
    try:
        bridge.run(stop, once=args.once)
    except OSError as e:                          # its /metrics port is taken, say
        _log(args.bridge, str(e))
        return 2
    _log(args.bridge, "stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
