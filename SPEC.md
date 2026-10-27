# Bridges: design

A **bridge** is a machine outside Orchestra. It polls a system that carries requests and answers
between people and bots (an issue tracker, a chat, a wiki), and publishes what happened there as
real CloudEvents on a project's Orchestra bus. Orchestra counts these events like any others, by
type and by source, so flows and hand-offs come without extra work. The design comes from
Orchestra's `docs/proposals/event-sources.md` §4–§6 and the owner's answers at the end of it.
This repository holds the bridges. Orchestra holds no bridge (its rule 1).

## 1. The event

- **Type**: the system first, then what happened: `github.issue.opened`, `github.issue.commented`,
  and so on (the README's table), so a later bridge's types (`jira.issue.created`) never mix with
  these in Orchestra's counts by type. Events published before the prefix (`issue.opened`) are
  still read back on a restart (`translate.current`).
- **Source**: who acted, as a machine (`identity.py`). Someone listed in the owner's identity map
  is `person.<key>`, or `bot.<key>` for a bot, whichever system they acted in: only the map joins
  accounts across systems. Anyone else is known by their account on that system: `person.<login>`
  or `bot.<login>` on GitHub (as first published), `person.slack.<handle>` or `bot.slack.<handle>`
  on Slack, so two strangers who share a login on two systems are two machines. Later bridges name
  their system the same way. The source is not pseudonymous: the owner chose people by name, each
  with a team.
- **Subject**: the item, e.g. `microsoft/vscode#1234`. Orchestra infers hand-offs from subjects.
- **Time and id**: the system's own. The id is built from the system's id for the occurrence
  (`github:<repo>:comment:<id>`), so it is the same however many times it is read.
- **Data**: metadata only, such as the repository, number, login, association, label name, close
  reason and comment id. **Never text**: titles, bodies and comments pass through memory and are
  never published, written or logged. A test checks this with a canary string.

Orchestra reads the envelope and never the data (its rule 3). The data is there for machines that
listen.

## 2. People as machines

The bridge announces each actor once, just before their first event: an
`orchestra.machine.started` whose source is the actor and whose time is that first event's time.
It carries `kind` (`person`, or `program` for a bot), `mode` (`ad hoc`), `package` (`bridges`),
`name` and `team`, which Orchestra reads (the team is the machine's group: a label that groups
people on the Machines page, never separates them), plus the account and `bot`, which Orchestra
passes to the package's card without reading. A person is announced again whenever what the
bridge would announce differs from their last announcement on the bus: before their next event,
and on start for everyone the map lists who was announced before, so a team changed in the map
shows even if they do nothing. Someone the map lists but the bridge never saw is not announced.
Every bridge does this through one module (`announce.py`).

The bridge itself is the machine `bridges.<system>`, by its events: it announces itself
(continuous, with its log) when it starts, and a system's own doing (an expiry, a gap) has it as
the source. The plugin also declares it (`plugin.py`), a placeholder so the Machines page lists it
before it is first seen. Its series do not make it a machine, and say nothing about which flows it
influences: they are measurements of the system (Orchestra's rule 7), and Orchestra infers any
association between a machine and a flow from the machine's events, never from a label. The GitHub
bridge writes its series itself and labels them `machine="bridges.github"`, which is provenance
(who wrote them) and how it finds its own series again; the Slack bridge's are stored by an
instrument and labelled `instrument="bridges.slack"` (§5).

## 3. No state of its own

A bridge keeps nothing outside the bus and the metrics store (the proposal's rule 5). What follows
is the GitHub bridge's way; the Slack bridge's differences are in §5. On start, the GitHub bridge
reads its own events back from the bus (ids starting `github:`), and from them it learns:

- which events are already published, so they are skipped from then on;
- its counts, rebuilt by replaying those events in time order, each counter then continuing from
  the value last written to the store (a trimmed bus, or a different order, never makes a counter
  go down, which discovery would read as a reset);
- where to read from next: the later of its last event and its last measurement (for the GitHub
  bridge, which writes its points itself, the store's latest point is the last poll), less
  `overlap`. With nothing on the bus, it reads from `lookback` ago.

The GitHub bridge publishes an event as soon as it is read but counts it only once it is older than
`overlap`, so an event GitHub shows late (a comment stamped before a close already read) is
counted in time order, as a restart's replay counts it. (The Slack bridge counts at once: §5.)

**Gaps** (Orchestra runs on a laptop that sleeps). A poll that succeeds more than three rounds
after the last good one publishes `bridge.gap` {repository, from, to, recovered} from the bridge's
own source, once, from the last good poll to this one (failed polls in between publish nothing),
and reads back to where it left off, but no further than `max_catchup`. `recovered` says whether the whole
gap was read. Caught-up events carry their own times, and so do the points written for them,
because nothing was written during the gap. Otherwise a point is never written before the latest
one for its series. A late change is stamped at that latest point instead, since a store reads a
series as its last value at or before each instant.

## 4. The GitHub issues bridge (`orchestra_bridges/github/`)

| Module | What it does |
|---|---|
| `api.py` | Read-only REST client. Follows `Link` pages, honours `Retry-After` and the primary rate limit, keeps the token in memory and sends it only to the configured host |
| `poll.py` | One read of a repository since a moment: the issues list (`since`, by update), the comments list (`since`, by update), and the issue events (newest first, until older) |
| `translate.py` | API objects to events: types, ids, metadata-only data; pull requests dropped |
| `responses.py` | First response: issues waiting for anyone but their author, and the waits of answered ones |
| `measures.py` | Writes those counts to the store at the time of each change, never before the last point |
| `bridge.py` | Resume, poll, announce, publish, record gaps, the loop |

**First response.** An issue the bridge saw opened waits until someone other than its author
comments on it or closes it. A label does not answer. A bot answers only with `bots_answer`. A
close by the author, or by a bot that does not count, ends the wait unanswered. Issues opened
before the bridge watched are not counted. The level and the counters that move it are written
together at each change, so they balance exactly:
`unanswered = opened − answered(all within) − closed_unanswered`. The response-time distribution
is counters by bucket (`within`), because Orchestra's collectors keep no histogram.

## 5. The Slack pings bridge (`orchestra_bridges/slack/`)

| Module | What it does |
|---|---|
| `token.py` | The user token: `SLACK_USER_TOKEN`, or a file outside the repository readable by its owner only |
| `api.py` | Read-only Web API client (`auth.test`, `search.messages`, `reactions.list`). Honours a `Retry-After` up to one poll long, on the stop event; sends the token only to the configured host, which `config.py` allows to be slack.com or a loopback fake only |
| `search.py` | The searches, newest first until older than the window. A match's text is checked for mentions in memory and dropped |
| `pings.py` | What a ping is (a DM, or a mention of the owner or a configured group, from someone else), ids, subjects |
| `waits.py` | The waiting pings and the counters, as Prometheus text |
| `serve.py` | `/metrics`, for the instrument the plugin declares |
| `bridge.py` | Identify, resume, read, publish, record gaps, the loop |

**Answered** is a checkmark reaction by the owner by default (the owner's answer, 2026-10-26). The
owner's first message after the ping in its thread (the reply rule) is the alternative; both can be
on. A checkmark answers what a reply in the same place would: the ping and the earlier ones still
waiting in its thread, or, for a ping not in a thread, the earlier ones in its channel not in a
thread either. When both rules see an answer in the same poll, the reply wins (it has a time).
Slack keeps no time for a reaction, so a checkmark answer is stamped when first seen, with
`not_before` (the last moment the ping could have been seen unanswered) in its data. When that is
more than two polls back (a gap, a first start) its wait is not known: the answer is counted
`within="unknown"` and left out of the summed waits. The `within` buckets are exclusive, and the
promise is (5m + 15m) / (5m + 15m + 60m + over). Waits run on the clock: working hours are not
modelled (an open question for the owner).

**It serves its counts; it does not write them.** Unlike the GitHub bridge, which writes to the
store through `orchestra.metrics`, the Slack bridge serves `/metrics` (the proposal's §5), and an
instrument stores them: the scrape the plugin declares (`Plugin.collectors`, `plugin.py`), which
`orchestra run` runs as `bridges.slack` every 30 s at `[bridges.slack] host` and `port` (a wildcard
host is scraped on loopback; with `port = 0` there is no fixed port, so none is declared). Every
point it stores is labelled `instrument="bridges.slack"` and the workspace. A project's own
`[[scrape]]` named `bridges.slack` replaces the plugin's (to scrape more often, say); under any
other name the bridge would not find its counts again. On start the bridge reads the instrument's
last values back through `orchestra.metrics`, by `instrument="bridges.slack"` and the workspace,
and continues each counter from the larger of the rebuilt and the stored value, so neither a
trimmed bus nor a scrape that missed the last change takes a counter down or loses a count. Points
stored before Orchestra's instruments (2026-10-27) are labelled `machine="bridges.slack"` instead;
the bridge reads those too and takes the higher value, so the upgrade, which starts each series over
under its new label, loses no count. The cost of serving: counts are stamped at the scrape, not at
Slack's times, so a gap's catch-up shows in the series at the moment the bridge woke. The events
keep the true times.

**Where it left off.** A scrape stores the served counts whether or not polls succeed, so the time
of the store's latest point is not the last poll (as it is for the GitHub bridge, §3). The bridge
serves `slack_bridge_last_poll_timestamp_seconds`, the start of its last successful poll, and on
start reads that value back: it is the last good poll for the gap test, and the later of it and
the last event, less `overlap`, is where reading resumes. A restart after an outage therefore reads
the outage back and records one `bridge.gap`. While it runs, the gap test measures from the end of
the last good poll, so a poll slowed by a `Retry-After` is not taken for a gap. It counts each event
as it publishes it (no `overlap` delay): the counts are served, not stamped at Slack's times.

**Reactions never block the searches.** Only `auth.test` and the searches fail a poll. The owner's
checkmarks come from `reactions.list` (the owner's reactions, most recent first, up to five pages
of 100 a poll), not one `reactions.get` per waiting ping, so the cost does not grow with the pings
waiting. If that read fails (a rate limit, the network, a scope the token lacks), the pings stay
waiting for the next poll, and everything else the poll read is published. A deleted ping is not
noticed: it expires like any other.

**`after:`** narrows each search by day in the searcher's own time zone, which the bridge does not
know, so it names the UTC date two days before the window: earlier than the window's start in
every zone from UTC-12 to UTC+14. The exact cut is each match's own time.

## 6. Running

`plugin.py` is the `orchestra.plugins` entry point `bridges`. For each bridge that has a section in
`orchestra.toml` (`[bridges.github]`, `[bridges.slack]`), it declares the process `orchestra run`
keeps running (`bridges.github`: `python -m orchestra_bridges github`, and so on) and a
placeholder for the machine that process is; for the Slack bridge, also the instrument that scrapes
its `/metrics` (§5). It has no pages. What a bridge shows is Orchestra's own Machines and
Instruments pages, map and series.

## 7. Tests

- `tests/fake_github.py`: a fake of the three REST lists in the real shapes, including the Link
  pagination to `/repositories/<id>/`, the bearer token, rate-limit headers and injected 403/429
  responses. Every title, body and comment carries the canary.
- `tests/unit`: identity, translation, first response, the client, the bridge against the fake
  (publishing, announcing, re-polls and restarts without duplicates, gaps, the canary and the
  token never leaving, failures), config and the plugin.
- `tests/e2e`: a real `orchestra run` in a scratch project starts the bridge from the plugin's
  declaration, the bridge publishes the fake's issues as their people, and stopping the host stops
  the bridge.
- `tests/fake_slack.py`: a fake of `auth.test`, `search.messages` and `reactions.list` with a
  scripted world on a clock (pings, replies and reactions appear when the clock reaches them),
  injected 429s with `Retry-After`, a body that is not JSON, and an asleep mode that drops every
  connection. Every message carries the canary.
- The e2e tests put this checkout first on `PYTHONPATH` for `orchestra run`, so the bridges it
  starts run the code under test.
- `tests/contract` (opt in, `ORCHESTRA_BRIDGES_CONTRACT=1`, and `ORCHESTRA_BRIDGES_SLACK_CONTRACT=1`
  for Slack, whose canary test also needs `SLACK_CANARY`): the real API for the fake. The fake
  must serve no key the real one lacks, the fields the bridge reads must match in type, and the
  translation must read real objects.

## 8. Not built yet

Jira, Confluence and Drive bridges; webhooks; a team-only mode; the
Pub/Sub preset. Counted traffic is built in Orchestra (its SPEC §4.4); the RabbitMQ preset and its
test rig are here (`orchestra_bridges/rabbitmq/`, README), and are configuration and a test
system, not bridges: they publish nothing on the bus.
