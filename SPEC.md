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
  is `person.<key>`, or `bot.<key>` for a bot, whichever system they acted in. Anyone else is
  `person.<login>` or `bot.<login>`, by their account on that system. The source is not
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

The bridge itself is the machine `bridges.<system>`. It announces itself (continuous, with its log)
when it starts, and it labels its series `machine="bridges.<system>"`. The plugin also declares it
(`plugin.py`), so the Machines page lists it before it is first seen.

## 3. No state of its own

A bridge keeps nothing outside the bus and the metrics store (the proposal's rule 5). On start, it
reads its own events back from the bus (ids starting `github:`), and from them it learns:

- which events are already published, so they are skipped from then on;
- its counts, rebuilt by replaying those events in time order, each counter then continuing from
  the value last written to the store (a trimmed bus, or a different order, never makes a counter
  go down, which discovery would read as a reset);
- where to read from next: the later of its last event and its last measurement (the store's
  latest point is the last poll), less `overlap`. With nothing on the bus, it reads from `lookback`
  ago.

An event is published as soon as it is read but counted only once it is older than `overlap`, so
an event GitHub shows late (a comment stamped before a close already read) is counted in time
order, as a restart's replay counts it.

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

## 5. Running

`plugin.py` is the `orchestra.plugins` entry point `bridges`. For each bridge that has a section in
`orchestra.toml` (`[bridges.github]`), it declares the process `orchestra run` keeps running
(`bridges.github`: `python -m orchestra_bridges github`) and the machine that process is. It has no
pages. What a bridge shows is Orchestra's own Machines page, map and series.

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
- `tests/contract` (opt in, `ORCHESTRA_BRIDGES_CONTRACT=1`): the real API for the fake. The fake
  must serve no key the real one lacks, the fields the bridge reads must match in type, and the
  translation must read real objects.

## 7. Not built yet

Jira, Slack (pings and checkmarks), Confluence and Drive bridges; webhooks; a team-only mode; the
Pub/Sub preset. Counted traffic is built in Orchestra (its SPEC §4.4); the RabbitMQ preset and its
test rig are here (`orchestra_bridges/rabbitmq/`, README), and are configuration and a test
system, not bridges: they publish nothing on the bus.
