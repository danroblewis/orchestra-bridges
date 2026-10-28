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
  or `bot.<login>` on GitHub (as first published, for issues and pull requests alike); a bridge
  after GitHub's puts its system in the name (`person.jira.<login>`, `person.slack.<handle>`), so
  two strangers who share a login on two systems stay two machines. The source is not
  pseudonymous: the owner chose people by name, each with a team.
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
(who wrote them) and how it finds its own series again; the pull-request, Jira and Slack bridges' are
stored by an instrument and labelled `instrument="bridges.<section>"` (§4b–§4d).

## 3. No state of its own

A bridge keeps nothing outside the bus and the metrics store (the proposal's rule 5). What follows
is the GitHub bridge's way; the Slack bridge's differences are in §4d. On start, the GitHub bridge
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
counted in time order, as a restart's replay counts it. (The Slack bridge counts at once: §4d.)

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

## 4b. The GitHub pull-request bridge (`orchestra_bridges/github_prs/`)

Goal G6 in Orchestra's `docs/roadmap/goals.md`: pull requests tracked to resolution. The owner's
CI/CD is driven by labels (the owner adds `SAIL`; the pipeline reports with labels), and whatever
wrote a change must be re-engaged when reviews, review comments, change requests or a failing label
arrive. Re-engaging is the task system's job. This bridge only reads and publishes, with the actor
as source and the pull request as subject, the events that wiring needs (README's table).

It is its own bridge, not an extension of the issues bridge: it reads other endpoints (the pulls
lists, per-pull-request timelines, checks), on another rhythm (every open followed pull request
every round, because a review does not always move `updated_at`), with its own filter, and it
serves its levels for an instrument where the issues bridge writes its own. It shares the GitHub
client (`github/api.py`, with `get` added for single objects), the identity map, and the announcer
(`announce.py`, shared by every bridge), so a person is one machine across both.

| Module | What it does |
|---|---|
| `poll.py` | The reads: open pulls, recently closed pulls, a timeline, a pull request's line comments, a commit, a head's check runs and statuses |
| `translate.py` | API objects to events: types `github.pr.*`, ids `github:<repo>:pr:<n>:<kind>:<GitHub id>`, metadata-only data with the branch and head |
| `levels.py` | What waits on whom, from GitHub's state now, and its Prometheus text |
| `bridge.py` | Resume, follow, read then publish, announce, gaps, `/metrics`, the loop |

**Following.** A pull request whose branch matches a glob of `branches` or that carries a label of
`labels` (all of them when neither is set) is followed until merged or closed. A closed one is
taken up only if opened inside the window read, so a first start publishes no history.

**Read, then publish.** A poll reads everything first (the open list, the closed list back to the
window's start, each followed pull request's timeline, its line comments when it has a new review,
the new head's commit, the head's checks) and publishes only when every read succeeded.

**Pushes.** A head not among the heads the bridge has published for that pull request is a push.
The heads come back from its own `opened` and `pushed` events on a restart. When a pull request is
first seen, its head at opening is taken from the timeline (the last commit dated no later than the
opening), so pushes made before the first poll are not lost, only merged into one. Who pushed comes
from GitHub's record of a force push, else the head commit's GitHub account, else the pull
request's author (`pusher_from`).

**Reviews.** Each submitted review is one `review_submitted` with its state and the number of its
line comments, then one `review_commented` per line comment, at the review's time (when they became
visible). A reply to a line comment is a review of its own on GitHub, so it arrives the same way.

**Levels**, not counts: each round they are read from GitHub, not rebuilt from events, and served
on `/metrics`; the plugin declares the scrape (`bridges.github_prs`, every 30 s). On start the
bridge reads its last good poll back from the store by `instrument="bridges.github_prs"`. Flows are
its events, which Orchestra counts by type.

## 4c. The Jira bridge (`orchestra_bridges/jira/`)

Building block 4 of Orchestra's `docs/roadmap/story-placement.md`: ten of the owner's user stories
need to know what happens on Jira issues (a request filed, work assigned to the owner, a CCB ticket
approved, a customer ticket's SLA, tickets in progress with no activity). The owner's Jira is **Data
Center**, so the bridge speaks REST API v2 with a personal access token first; Cloud (API v3, an API
token with the account's email) costs only another search paging and sign-in, so it stays.

| Module | What it does |
|---|---|
| `api.py` | Read-only client: Bearer (Data Center) or Basic (Cloud), `startAt` or `nextPageToken` searches, an issue's whole changelog and comments, `Retry-After` on 429 and 503, the token sent only to the site's origin |
| `records.py` | Jira's JSON as metadata-only records, made where it is read: ids, keys, accounts, names an administrator defines, label names, sprint ids, times. Text is dropped here |
| `poll.py` | The reads: who the owner is, the sprint field and the statuses (`site`); changed issues (`updated`); open issues, the done count and the owner's open issues with their comments (for the levels) |
| `translate.py` | Records to events: types `jira.*`, ids `jira:<site>:issue:<issue id>:…`, subject the key |
| `levels.py` | Issues by status category, the owner's not started, the owner's issues with an unanswered comment and the oldest wait, and their Prometheus text |
| `bridge.py` | Resume, read then publish, announce, gaps, `/metrics`, the loop |

The shared `secret.py` reads its token (the environment, else a `chmod 600` file outside the
repository), as the Slack bridge's does.

**Following.** The issues of `projects`, or of a `jql` clause, on one site. A change is found by
JQL `updated >= "-Nm"` (relative minutes, by Jira's clock), with the changelog expanded; a
changelog or comment list the search cut short is read whole from the issue. Of each issue the
bridge publishes its creation (and an assignee set at creation), every change of status or assignee,
every change of sprint, priority, labels, links or due date (one `jira.issue.updated` per change
set), and every comment, each once, by its Jira id. A change with no author is Jira's own and has the
bridge as source. A second site is a second section, `[bridges.jira_<name>]`: its own process,
machine, instrument, port and token; the identity map's `jira` key serves both.

**People.** On Data Center an account is its username (the map may list the username or the user
key); on Cloud, its account id, and an app account is a bot. Someone the map does not list is
`person.jira.<account>`, announced with Jira's display name (the owner chose people by name). The
owner is whoever the token belongs to (`/myself`).

**Levels**, read from Jira each round and served on `/metrics` with the label `jira_project`
(`project` is Orchestra's): issues by status category (`done` every `done_every`), the owner's
issues not started, the owner's open issues with a comment the owner has not answered and how long
the oldest has waited, and the last good poll, which the bridge reads back by
`instrument="bridges.<section>"` on start. The plugin declares the scrape.

**Read only.** Every request is a GET. Commenting, transitioning and creating issues as the owner is
the Outbox's, which the Supervisor configures; this bridge's keys and the identity map are named so
the Supervisor can configure them (README).

## 4d. The Slack pings bridge (`orchestra_bridges/slack/`)

| Module | What it does |
|---|---|
| `api.py` | Read-only Web API client (`auth.test`, `search.messages`, `reactions.list`). Honours a `Retry-After` up to one poll long, on the stop event; sends the token only to the configured host, which `config.py` allows to be slack.com or a loopback fake only |
| `search.py` | The searches, newest first until older than the window. A match's text is checked for mentions in memory and dropped |
| `pings.py` | What a ping is (a DM, or a mention of the owner or a configured group, from someone else), ids, subjects |
| `waits.py` | The waiting pings and the counters, as Prometheus text |
| `bridge.py` | Identify, resume, read, publish, record gaps, the loop |

It shares with the other bridges the token reading (`secret.py`: `SLACK_USER_TOKEN`, else a
`chmod 600` file outside the repository, by default `~/.config/orchestra-bridges/slack-token`),
the `/metrics` server (`serve.py`), the announcer (`announce.py`) and the identity map.

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

## 5. Running

`plugin.py` is the `orchestra.plugins` entry point `bridges`. For each bridge that has a section in
`orchestra.toml` (`[bridges.github]`, `[bridges.github_prs]`, `[bridges.slack]`, `[bridges.jira]`,
`[bridges.jira_<name>]`),
it declares the process `orchestra run` keeps running (`bridges.<section>`: `python -m
orchestra_bridges <section>`) and the machine that process is. For the pull-request, Slack and Jira bridges
it also declares the instrument that scrapes its `/metrics` (`Plugin.collectors`), unless its `port`
is 0. It has no pages. What a bridge shows is
Orchestra's own Machines and Instruments pages, map and series.

## 6. Tests

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
- `tests/contract` (opt in, `ORCHESTRA_BRIDGES_CONTRACT=1` for GitHub, `ORCHESTRA_BRIDGES_SLACK_CONTRACT=1`
  for Slack, whose canary test also needs `SLACK_CANARY`): the real API for each fake. A fake
  must serve no key the real one lacks, the fields the bridge reads must match in type, and the
  translation must read real objects.
- `tests/fake_github_prs.py`, `tests/unit/test_github_prs_*.py`, `tests/e2e/test_github_prs_run.py`:
  the pull-request bridge against a fake of the pulls, timeline, comments, commit and checks
  endpoints, and through `orchestra run` with its instrument.
- `tests/contract/test_github_prs_sandbox.py` (opt in, `ORCHESTRA_BRIDGES_SANDBOX=1`): a real pull
  request on the private sandbox `danroblewis/orchestra-e2e-sandbox`, opened, reviewed with a line
  comment, labelled and unlabelled, given a failing status, pushed to and merged with `gh`; each
  event must be published once as the owner on the pull request, and a restart after a simulated
  gap must record the gap and publish nothing twice.

- `tests/fake_jira.py`, `tests/unit/test_jira_*.py`, `tests/e2e/test_jira_run.py`: the Jira bridge
  against a fake of Data Center's REST API v2 (and Cloud's v3) in the real shapes: searches paged
  both ways with the changelog, an issue's changelog and comments, `/myself`, `/field`, `/status`,
  the JQL the bridge writes, 429 with `Retry-After`, and an "asleep" mode that drops every
  connection. Every summary, description, comment body, sprint goal and text change carries the
  canary, which no event, log line, metric or file may hold.
- `tests/contract/test_jira_contract.py` (opt in, `ORCHESTRA_BRIDGES_JIRA_CONTRACT=1` with
  `JIRA_URL` and `JIRA_API_TOKEN`): the fake's shapes against a real Jira. Not yet run: there is no
  account.

## 7. Not built yet

Confluence and Drive bridges; webhooks; a team-only mode; the
Pub/Sub preset. Counted traffic is built in Orchestra (its SPEC §4.4); the RabbitMQ preset and its
test rig are here (`orchestra_bridges/rabbitmq/`, README), and are configuration and a test
system, not bridges: they publish nothing on the bus.
