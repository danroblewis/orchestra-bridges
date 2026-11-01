# jira_poller

Polls Jira Data Center (REST API v2, a personal access token) every 5 minutes for the issues in
scope (`[settings] jql`) updated since its last poll, with their changelog.

## What it emits

On the project's bus, once each (the id is `jira_poller:<type>:<key>@<stamp>`), with the issue key
as the subject and **who acted** as the source: `person.jira.<login>`, or `bot.jira` when no one did.

| type | when | stamp | data |
|---|---|---|---|
| `jira.issue.created` | an issue created since the poller began | `created` | `status` (its first) |
| `jira.issue.transitioned` | a status change | the change's id | `from`, `to` (status names) |
| `jira.issue.updated` | any other change | the change's id | `fields` (names, never values) |

Each actor is announced once (`orchestra.machine.started`, kind `person`, mode `ad hoc`). The poller
never publishes as itself.

## What it measures

`python jira_poller.py measure` prints, for its `[[collect]]` entry:

- `jira_issues{status}` (gauge): the issues in scope in each status: a level. A status
  emptied reads 0.
- `jira_issue_age_seconds{status}` (gauge): seconds since the oldest issue not done in the status
  was created. An age, not a quantity: discovery never makes it a stock.
- `jira_issues_created_total{status}`, `jira_issue_transitions_total{from_status, to_status}`
  (counters): issues created into a status, and moved from one to another (one counter per move, so
  discovery joins the status left to the status entered as one flow), counted by the poller as it
  relays and kept with its cursor. They balance `jira_issues`: Orchestra's own count of the events
  (`orchestra_events_total{type, source}`) cannot, because it never reads `data` (the status).

## Run it

Copy this directory into the project (its state directory, `<state>/machines/jira_poller/`, by
default), set `base_url` and `jql`, export `JIRA_API_TOKEN`, and have the owner approve
(`clearance = "approved"`, `data_class`). The Supervisor adds, through `config_edit`:

```toml
[[machines]]
name = "jira_poller"
command = "python <dir>/jira_poller.py"
restart = "always"

[[collect]]
name = "jira_poller"
command = "python <dir>/jira_poller.py measure"
every = "60s"
timeout = "30s"
```

Test: `uv run python -m pytest examples/integrations/jira_poller -q` (two intervals of the running
poller against `FakeJira`, a restart, `measure`, the canary, the clearance gate).
