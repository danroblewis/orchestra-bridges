"""Run a bridge for the project at ORCHESTRA_REPO, else the working directory, until SIGTERM or SIGINT.

``orchestra bridges github|github_prs|slack|jira[_<name>] [--once]`` (or ``orchestra bridges scaffold
<kind> <name>``, which writes a new machine from a kit: ``kits/scaffold.py``; ``orchestra bridges
integrations``, which lists them: ``kits/integrations.py``) (``orchestra --repo PATH bridges
...`` sets ORCHESTRA_REPO), or ``python -m orchestra_bridges ...``, which the launcher runs and gives
ORCHESTRA_REPO. A bridge publishes only as the people and bots who acted (``relay.py``), so its bus has no
source of its own."""

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
    return github.Bridge(conf, api, Events.for_project(project),
                         ProjectStore(for_project(project), project.key))


def _github_prs(project, stop: threading.Event):
    conf = cfg.github_prs(project.config.raw, project.repo_root)
    api = GitHub(gh_token(conf.api), conf.api)
    return github_prs.Bridge(conf, api, Events.for_project(project),
                             ProjectStore(for_project(project), project.key))


def _jira(project, name: str, stop: threading.Event):
    conf = cfg.jira(project.config.raw, project.repo_root, name)
    token = read_token(conf.token_env, f"{conf.name}-token", project.repo_root, conf.token_file)
    email = os.environ.get(f"{conf.env}_EMAIL") or conf.email
    api = Jira(conf.api, token, deployment=conf.deployment, email=email, sleep=stop.wait)
    return jira.Bridge(conf, api, Events.for_project(project),
                       ProjectStore(for_project(project), project.key))


def _slack(project, stop: threading.Event):
    """A Retry-After is waited out on ``stop`` (so SIGTERM ends it) and for no longer than a poll."""
    conf = cfg.slack(project.config.raw, project.repo_root)
    token = read_token(conf.token_env, "slack-token", project.repo_root, conf.token_file)
    api = Slack(token, conf.api, sleep=stop.wait, max_wait=conf.every)
    return slack.Bridge(conf, api, Events.for_project(project),
                        ProjectStore(for_project(project), project.key))


BRIDGES = {"github": (_github, github), "github_prs": (_github_prs, github_prs), "slack": (_slack, slack)}


def _log(name: str, msg: str) -> None:
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} bridges.{name}: {msg}", file=sys.stderr, flush=True)


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["scaffold"]:                  # a new Programmatic Machine from a kit (kits/scaffold.py)
        from .kits.scaffold import main as scaffold
        return scaffold(argv[1:])
    if argv[:1] == ["integrations"]:              # read-only: the scaffolded machines (kits/integrations.py)
        from .kits.integrations import main as integrations
        return integrations(argv[1:])
    ap = argparse.ArgumentParser(prog="orchestra bridges")
    ap.add_argument("bridge", help=f"{', '.join(sorted(BRIDGES))}, jira, or jira_<name> (or: scaffold <kind> <name>, integrations)")
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
