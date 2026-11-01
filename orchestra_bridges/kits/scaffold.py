"""``orchestra bridges scaffold <kind> <name> [--root repo|state|overlay]``: a new collector (a poller
that relays what people and bots did in another system, and its measurement) from a kit template
(``templates/<kind>/``), written as

    <root>/machines/<name>/{machine.toml, <name>.py, test_<name>.py, AGENTS.md}

The roots:

- ``repo``: ``.orchestra/machines/<name>/`` in the owner's repository (the default: the owner,
  2026-11-01, "scripts in the target repo"), where a coding tool adapts it and a pull request
  lands it (this command only writes files, never commits);
- ``state``: ``<state>/machines/<name>/``, the project's state directory, only when the owner
  says the repository must stay clean;
- ``overlay``: ``machines/<name>/`` beside the private overlay (``ORCHESTRA_CONFIG_OVERLAY``, else
  the state directory's ``orchestra.local.toml``): a private repository of the owner's, when set.

It writes nothing else: the ``[[machines]]`` and ``[[collect]]`` entries that run the poller and
store its measurement are printed (and in its AGENTS.md) for the Supervisor to add through
``config_edit`` once the owner approved (``approve_integration`` writes the same two). Their
commands start with ``python`` (Orchestra's own, as the launcher runs it, from the repository's
root), so a script in the repository is named by its path there. Their ``every`` is the
template's own (``[settings] every``, 5 minutes). An existing directory is never overwritten.
``examples/integrations/scaffolded/`` holds what each kit writes, as the reference."""

from __future__ import annotations

import argparse
import re
import shlex
import sys
from dataclasses import dataclass
from pathlib import Path

NAME = re.compile(r"^[a-z][a-z0-9_]{1,40}$")
TEMPLATES = Path(__file__).parent / "templates"
ROOTS = ("repo", "state", "overlay")
COLLECT_TIMEOUT = "60s"
SECTION = re.compile(r"\{\{([#^])poller\}\}(.*?)\{\{/poller\}\}", re.DOTALL)


@dataclass(frozen=True)
class Kind:
    system: str
    token_env: str
    fake: str
    events: str
    series: str
    poller: bool = True                 # False: a measurement only (a metrics store has no item events)
    key: str | None = None              # the sources' and types' first word: the kit's name unless said


KINDS = {
    "github": Kind("GitHub", "GITHUB_TOKEN", "FakeGitHub",
                   "one vocabulary, the one the Workflows plugin's examples wait on: `github.pr.opened`, "
                   "`.synchronize`, `.labeled`, `.unlabeled`, `.review_requested`, `.approved`, `.changes_requested`, "
                   "`.commented`, `.merged` (and `.closed`, `.reopened`), `github.checks.completed` (data: outcome), "
                   "and `github.issue.opened`, `.labeled`, `.unlabeled`, `.closed`, `.reopened` (subject: "
                   "`<owner>/<name>#<number>`; data: repository, number; source: `person.github.<login>`, or "
                   "`bot.github.<app>` for a bot or app). Keep these names: workflows wait on them",
                   "`github_issues_open` and `github_prs_open{review}` (the open pull requests followed, by review state)"),
    "pagerduty": Kind("PagerDuty", "PAGERDUTY_TOKEN", "FakePagerDuty",
                      "`pagerduty.incident.triggered`, `.acknowledged`, `.resolved` (subject: the incident's id; "
                      "source: `person.pagerduty.<user id>`, or `bot.pagerduty` when the service changed it)",
                      "`pagerduty_incidents_open{service, urgency}` (the open incidents; a sum over its labels needs no series of its own)"),
    "jira": Kind("Jira (Data Center or Cloud: `[settings] flavour`)", "JIRA_API_TOKEN", "FakeJira",
                 "`jira.issue.created`, `jira.issue.assigned` and `jira.issue.transitioned` once per change (subject: "
                 "the issue key; source: `person.jira.<account>`; data: the status, the assignee's account, from and to)",
                 "`jira_issues{status, category}` (the issues in scope by status)"),
    "slack": Kind("Slack", "SLACK_TOKEN", "FakeSlack",
                  "`slack.message.posted` (subject: `<channel>/<thread ts>`; source: `person.slack.<member id>` or "
                  "`bot.slack.<id>`; data: channel, reply or not, bot or not)",
                  "`slack_channel_messages_last_hour{channel}`"),
    "http": Kind("a JSON API no kit knows", "API_TOKEN", "FakeAPI (a FakeServer written in the test)",
                 "`<system>.<thing>.<state>` once per item change (subject: the item's id; source: "
                 "`person.<system>.<account>`, or `bot.<system>` when no one; settings name the fields)",
                 "`<system>_<thing>s{state}` (the items listed, by state)", key="<system>"),
    "promql": Kind("a Prometheus API (Prometheus, Chronosphere, Mimir)", "PROMETHEUS_TOKEN", "FakePrometheus",
                   "none: a metrics store has no item events, so it has no poller and no `[[machines]]` entry",
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


def every_of(machine_toml: str) -> str:
    """The template's ``[settings] every`` as written (``"5m"``), for the entries' rhythm."""
    import tomllib
    v = (tomllib.loads(machine_toml).get("settings") or {}).get("every", "5m")
    return v if isinstance(v, str) else f"{v}s"


def entries(name: str, shown: str | Path, poller: bool = True, every: str = "5m") -> str:
    """The ``[[machines]]`` entry that keeps the poller running (when it has one) and the
    ``[[collect]]`` entry that runs its measurement, for the Supervisor to add through ``config_edit``.
    ``shown``: the directory as the commands name it (relative to the repository for ``--root repo``)."""
    prog = shlex.quote(f"{shown}/{name}.py")
    process = [
        "[[machines]]                      # the poller's process (a relay: its actors are the machines)",
        f'name = "{name}"',
        f"command = {_toml(f'python {prog}')}",
        'restart = "always"',
        "",
    ] if poller else []
    return "\n".join([
        *process,
        "[[collect]]                       # the instrument that stores its measurement",
        f'name = "{name}"',
        f"command = {_toml(f'python {prog} measure')}",
        f'every = "{every}"',
        f'timeout = "{COLLECT_TIMEOUT}"',
    ])


def _toml(s: str) -> str:
    return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'


def render(text: str, values: dict[str, str], poller: bool = True) -> str:
    """The template with its values, and its ``{{#poller}}…{{/poller}}`` parts kept only for a kit
    with a poller (``{{^poller}}…{{/poller}}`` only for one without)."""
    text = SECTION.sub(lambda m: m.group(2) if (m.group(1) == "#") == poller else "", text)
    for k, v in values.items():
        text = text.replace("{{" + k + "}}", v)
    return text


def scaffold(kind: str, name: str, target_root: Path, shown_root: str | Path | None = None) -> Path:
    """Write the machine into ``target_root/machines/name``; return its directory. ``shown_root``:
    the root as the files name it (``.orchestra`` for the repository's), else ``target_root``."""
    if kind not in KINDS:
        raise ScaffoldError(f"no kit {kind!r}: the kits are {', '.join(sorted(KINDS))}")
    if not NAME.match(name):
        raise ScaffoldError(f"{name!r}: a poller's name is lower case letters, digits and _ (2 to 41), "
                            "starting with a letter")
    target = target_root / "machines" / name
    if target.exists():
        raise ScaffoldError(f"{target} exists: choose another name, or remove it first")
    shown = f"{shown_root if shown_root is not None else target_root}/machines/{name}"
    k, src = KINDS[kind], TEMPLATES / kind
    values = {"name": name, "kind": kind, "system": k.system, "system_key": k.key or kind, "token_env": k.token_env,
              "fake": k.fake, "events": k.events, "series": k.series, "dir": shown}
    values["entries"] = entries(name, shown, k.poller, every_of((src / "machine.toml").read_text(encoding="utf-8")))
    files = {"machine.toml": src / "machine.toml", f"{name}.py": src / "machine.py",
             f"test_{name}.py": src / "test_machine.py", "AGENTS.md": TEMPLATES / "AGENTS.md"}
    target.mkdir(parents=True)
    for out, tpl in files.items():
        (target / out).write_text(render(tpl.read_text(encoding="utf-8"), values, k.poller), encoding="utf-8")
    return target


def shown_root(project, root: str) -> str | Path:
    """How the files and the entries name the root: the repository's relative to it (the launcher
    runs commands from the repository's root), the others by their full path."""
    return ".orchestra" if root == "repo" else root_dir(project, root)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="orchestra bridges scaffold",
                                 description="Write a new collector from a kit template.")
    ap.add_argument("kind", choices=sorted(KINDS))
    ap.add_argument("name", help="the poller's name: its process, its instrument and its state (lower case, digits, _)")
    ap.add_argument("--root", choices=ROOTS, default="repo",
                    help="where it goes: the repository's .orchestra/machines/ (default), the state directory "
                         "(when the repository must stay clean), or beside the private overlay")
    a = ap.parse_args(argv)
    from orchestra.config import ConfigError, load_project
    try:
        project = load_project()
        shown = shown_root(project, a.root)
        target = scaffold(a.kind, a.name, root_dir(project, a.root), shown)
    except (ConfigError, ScaffoldError, OSError) as e:
        print(f"scaffold: {e}", file=sys.stderr)
        return 2
    k = KINDS[a.kind]
    print(f"wrote {target}/ (machine.toml, {a.name}.py, test_{a.name}.py, AGENTS.md)")
    # Orchestra's own Python: it has the kits and pytest; the repository's own environment may have neither
    print(f"test it:  {shlex.quote(sys.executable)} -m pytest {shlex.quote(str(target / f'test_{a.name}.py'))} -q")
    print("run it for good, once the owner approved: these entries through config_edit (the overlay by default):\n")
    print(entries(a.name, f"{shown}/machines/{a.name}", k.poller,
                  every_of((target / "machine.toml").read_text(encoding="utf-8"))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
