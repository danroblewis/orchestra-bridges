# Integration examples

Four pollers built from the kits (`orchestra_bridges/kits/`, SPEC §7; Orchestra's
`docs/INTEGRATING.md`), each written by `orchestra bridges scaffold <kit> <name>` and then completed.
They are examples to copy, not part of the plugin: nothing here is installed or declared by it.

| example | kit | publishes (as the actors) | measures (`measure`, for `[[collect]]`) |
|---|---|---|---|
| [`jira_poller`](jira_poller/README.md) | jira | `jira.issue.created`, `.transitioned`, `.updated` | `jira_issues{status}`, `jira_issue_age_seconds{status}`, moves in and out of each status |
| [`slack_watcher`](slack_watcher/README.md) | slack | `slack.message.posted` (all, or only mentions) | `slack_messages_total{channel}`, `slack_mentions_open`, mentions opened and answered |
| [`pagerduty_alerts`](pagerduty_alerts/README.md) | pagerduty | `pagerduty.incident.triggered`, `.acknowledged`, `.resolved` | `pagerduty_incidents_open{urgency}`, incidents opened and closed |
| [`chronosphere_series`](chronosphere_series/README.md) | promql | nothing (a store has no item events) | a `[[sources]]` entry (preferred), or a few PromQL answers as gauges |

Every one keeps the kits' rules: the token by environment name only (or a `chmod 600` file outside
any repository), never logged; `clearance = "needs approval"` in `machine.toml` reads only a fake on
this machine until the owner approves; each event published once, across restarts too (its id is
the item's id and its change's stamp; the poller's counters are kept with its cursor); metadata
only, never text. The fakes need no credentials.

A poller is a relay: its events' source is the person or bot who acted, each announced once, so
the Machines page shows those actors, never the poller. Its series are measurements (stored by the
`[[collect]]` entry that runs `measure`, an instrument), so discovery can find the stocks: the open
incidents, the issues in each status, the open mentions; an age is never a stock.

**Name a gauge for what it holds, never `_total`.** `_total` is a counter's suffix (Prometheus,
OpenMetrics), and Orchestra's discovery reads a series named `_total` that never falls (but for the
odd reset) as a counter. A level that only rises in the window, such as the issues done, would
then be taken for a flow and never found as a stock (in these examples' first version,
`jira_issues_total{status="Done"}` became the flow out of In Review). So the levels here are
`jira_issues{status}`, `pagerduty_incidents_open{urgency}` and `slack_mentions_open`, and only the
counters end in `_total`.

Tests: each example's own (`test_<name>.py`, against its fake), discovery over the examples'
series (`tests/unit/test_integrations_model.py`), and `orchestra run` with all three pollers and
the Machines page (`tests/e2e/test_integrations_run.py`).
