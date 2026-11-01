# pagerduty_alerts

Polls PagerDuty's REST API v2 (a read-only API key, `Token token=`) every 5 minutes: the open
incidents (triggered, acknowledged), and by id each one open last poll and not now.

## What it emits

`pagerduty.incident.triggered`, `.acknowledged`, `.resolved`, once per change (stamp: its
`last_status_change_at`), with the incident's id as the subject and **who changed it** as the
source: `person.pagerduty.<user id>`, or `bot.pagerduty` when the service did (a trigger from an
integration, an auto-resolve). Data: `number`, `urgency`, `service` (its id). Never a title.

## What it measures

`python pagerduty_alerts.py measure` prints, for its `[[collect]]` entry:

- `pagerduty_incidents_open_total{urgency}` (gauge): the open incidents of each urgency (`high`
  and `low` always printed, 0 included): a level;
- `pagerduty_incidents_triggered_total{urgency}`, `pagerduty_incidents_resolved_total{urgency}`
  (counters): the incidents first seen open and those that stopped being open, counted by the
  poller with its cursor. They balance the open incidents by urgency, which Orchestra's count of
  the events (by type and source) cannot split.

## Run it

As `jira_poller` (its README): export `PAGERDUTY_TOKEN`, the owner's clearance, then the
`[[machines]]` entry (`python <dir>/pagerduty_alerts.py`) and the `[[collect]]` entry
(`python <dir>/pagerduty_alerts.py measure`) through `config_edit`.

Test: `uv run python -m pytest examples/integrations/pagerduty_alerts -q`.
