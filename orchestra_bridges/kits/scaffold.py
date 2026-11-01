"""``orchestra bridges scaffold <kind> <name> [--root state|overlay|repo]``: a new poller (a relay
of what people and bots did in another system, and its measurement) from a kit template (``templates/<kind>/``), written as

    <root>/machines/<name>/{machine.toml, <name>.py, test_<name>.py, AGENTS.md}

The roots (machine-design.md §2.1, "the state directory by default"):

- ``state``: ``<state>/machines/<name>/``, the project's state directory (the default; where
  orchestra-agents reads the state directory's machine specs too);
- ``overlay``: ``machines/<name>/`` beside the private overlay (``ORCHESTRA_CONFIG_OVERLAY``, else
  the state directory's ``orchestra.local.toml``): a private repository of the owner's, when set;
- ``repo``: ``.orchestra/machines/<name>/`` in the repository, for a pull request (never committed
  to the base branch by this command: it only writes files).

It writes nothing else: the ``[[machines]]`` and ``[[collect]]`` entries that run the machine and
store its series are printed (and in its AGENTS.md) for the Supervisor to add through
``config_edit``. An existing directory is never overwritten."""

from __future__ import annotations

import argparse
import re
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path

NAME = re.compile(r"^[a-z][a-z0-9_]{1,40}$")
TEMPLATES = Path(__file__).parent / "templates"
ROOTS = ("state", "overlay", "repo")


@dataclass(frozen=True)
class Kind:
    system: str
    token_env: str
    fake: str
    events: str
    series: str
    poller: bool = True                 # False: a measurement only (a metrics store has no item events)


KINDS = {
    "pagerduty": Kind("PagerDuty", "PAGERDUTY_TOKEN", "FakePagerDuty",
                      "`pagerduty.incident.triggered`, `.acknowledged`, `.resolved` (subject: the incident's id; "
                      "source: `person.pagerduty.<user id>`, or `bot.pagerduty` when the service changed it)",
                      "`pagerduty_incidents_open{service, urgency}`, `pagerduty_incidents_open_total`"),
    "jira": Kind("Jira", "JIRA_API_TOKEN", "FakeJira",
                 "`jira.issue.transitioned` once per status change (subject: the issue key; source: "
                 "`person.jira.<login>`; data: from and to status)",
                 "`jira_issues{status, category}` (the issues in scope by status)"),
    "slack": Kind("Slack", "SLACK_TOKEN", "FakeSlack",
                  "`slack.message.posted` (subject: `<channel>/<thread ts>`; source: `person.slack.<member id>` or "
                  "`bot.slack.<id>`; data: channel, reply or not, bot or not)",
                  "`slack_channel_messages_last_hour{channel}`"),
    "promql": Kind("a Prometheus API (Prometheus, Chronosphere, Mimir)", "PROMETHEUS_TOKEN", "FakePrometheus",
                   "none: a metrics store has no item events, so it has no poller and no `[[machines]]` entry "
                   "(its `make_poll` does nothing)",
                   "one gauge per configured query, named by its `name`, with the answer's labels (aliases applied)",
                   poller=False),
}


class ScaffoldError(Exception):
    pass


def root_dir(project, root: str) -> Path:
    if root == "state":
        return project.state_dir
    if root == "overlay":
        return project.overlay_file.parent
    if root == "repo":
        return project.repo_root / ".orchestra"
    raise ScaffoldError(f"--root must be one of {', '.join(ROOTS)}, not {root!r}")


def entries(name: str, target: Path, poller: bool = True) -> str:
    """The ``[[machines]]`` entry that keeps the poller running (when it has one) and the
    ``[[collect]]`` entry that runs its measurement, for the Supervisor to add through ``config_edit``."""
    py, prog = shlex.quote(sys.executable), shlex.quote(str(target / f"{name}.py"))
    process = [
        "[[machines]]                      # the poller's process (a relay: its actors are the machines)",
        f'name = "{name}"',
        f"command = {_toml(f'{py} {prog}')}",
        'restart = "always"',
        "",
    ] if poller else []
    return "\n".join([
        *process,
        "[[collect]]                       # the instrument that stores its measurement",
        f'name = "{name}"',
        f"command = {_toml(f'{py} {prog} measure')}",
        'every = "60s"',
        'timeout = "30s"',
    ])


def _toml(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def render(text: str, values: dict[str, str]) -> str:
    for k, v in values.items():
        text = text.replace("{{" + k + "}}", v)
    return text


def scaffold(kind: str, name: str, target_root: Path) -> Path:
    """Write the machine into ``target_root/machines/name``; return its directory."""
    if kind not in KINDS:
        raise ScaffoldError(f"no kit {kind!r}: the kits are {', '.join(sorted(KINDS))}")
    if not NAME.match(name):
        raise ScaffoldError(f"{name!r}: a poller's name is lower case letters, digits and _ (2 to 41), "
                            "starting with a letter")
    target = target_root / "machines" / name
    if target.exists():
        raise ScaffoldError(f"{target} exists: choose another name, or remove it first")
    k, src = KINDS[kind], TEMPLATES / kind
    values = {"name": name, "kind": kind, "system": k.system, "token_env": k.token_env, "fake": k.fake,
              "events": k.events, "series": k.series, "dir": str(target)}
    values["entries"] = entries(name, target, k.poller)
    files = {"machine.toml": src / "machine.toml", f"{name}.py": src / "machine.py",
             f"test_{name}.py": src / "test_machine.py", "AGENTS.md": TEMPLATES / "AGENTS.md"}
    target.mkdir(parents=True)
    for out, tpl in files.items():
        (target / out).write_text(render(tpl.read_text(encoding="utf-8"), values), encoding="utf-8")
    return target


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="orchestra bridges scaffold",
                                 description="Write a new poller from a kit template.")
    ap.add_argument("kind", choices=sorted(KINDS))
    ap.add_argument("name", help="the poller's name: its process, its instrument and its state (lower case, digits, _)")
    ap.add_argument("--root", choices=ROOTS, default="state",
                    help="where it goes: the state directory (default), beside the private overlay, or the repository")
    a = ap.parse_args(argv)
    from orchestra.config import ConfigError, load_project
    try:
        project = load_project()
        target = scaffold(a.kind, a.name, root_dir(project, a.root))
    except (ConfigError, ScaffoldError, OSError) as e:
        print(f"scaffold: {e}", file=sys.stderr)
        return 2
    print(f"wrote {target}/ (machine.toml, {a.name}.py, test_{a.name}.py, AGENTS.md)")
    print(f"test it:  uv run python -m pytest {target}/test_{a.name}.py -q")
    print("run it for good: add these entries through config_edit (the overlay by default):\n")
    print(entries(a.name, target, KINDS[a.kind].poller))
    return 0


if __name__ == "__main__":
    sys.exit(main())
