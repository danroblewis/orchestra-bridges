# orchestra-bridges: architecture

This document explains how orchestra-bridges works and why it is built this way. It is written
for two readers: the owner, who runs it on a laptop that sleeps, and engineers new to the code.
`SPEC.md` is the design and `AGENTS.md` lists the rules. This document links them to the code,
which was checked statement by statement on 2026-10-27, and draws the moving parts. It was
brought up to date the same day with Orchestra's instruments (Orchestra `8344264c`: collectors and
scrapes are instruments, not machines), again when the pull-request, Jira and Slack bridges
were merged into `main`, and again when the bridges stopped being machines (Orchestra's decision
D19, "the provider is never the machine": §1).

Throughout, every part is marked by status:

- **Built (main)**: on `main`, tested. Every bridge in this document is: GitHub issues (§5), the
  Slack pings bridge (§7, merged from `slack-bridge`), GitHub pull requests (§12, merged from
  `pr-tracking`) and Jira (§13, merged from `jira-bridge`).
- **Proposed**: designed in Orchestra's `docs/proposals/event-sources.md`, not built.

## Contents

1. [What it is for](#1-what-it-is-for)
2. [Status at a glance](#2-status-at-a-glance)
3. [The parts](#3-the-parts)
4. [How it plugs into Orchestra](#4-how-it-plugs-into-orchestra)
5. [The GitHub issues bridge (main)](#5-the-github-issues-bridge-main)
6. [Counted traffic: the RabbitMQ preset and rig (main)](#6-counted-traffic-the-rabbitmq-preset-and-rig-main)
7. [The Slack pings bridge](#7-the-slack-pings-bridge)
8. [Contracts](#8-contracts)
9. [Add a new bridge](#9-add-a-new-bridge)
10. [Proposed, not built](#10-proposed-not-built)
11. [Where the docs and the code disagree](#11-where-the-docs-and-the-code-disagree)
12. [The GitHub pull-request bridge](#12-the-github-pull-request-bridge)
13. [The Jira bridge](#13-the-jira-bridge)

---

## 1. What it is for

Orchestra is an event bus, a metrics store and a system dynamics model of the system that uses
them. It knows a machine only by its events on the bus, or by counted traffic on another system's
bus (Orchestra's rules 1 and 2). Metrics are measurements of the system, taken by instruments; a
series never makes a machine, and a machine is associated with a flow only as a hypothesis
Orchestra infers from its events (rules 6 and 7). Much of the work that matters happens elsewhere: in issue trackers, in chat, and on other
companies' message brokers. This package brings that work in, in three ways:

| What we care about | How it arrives | Where |
|---|---|---|
| **Other systems' activity as machines and events.** An issue opened, a comment, a Slack ping and its answer | A **bridge** polls the system and publishes real CloudEvents on Orchestra's bus. The source is the actor and the subject is the item | `orchestra_bridges/github/`, `github_prs/`, `jira/`, `slack/` |
| **People as machines, with names and teams** | Each actor becomes an event `source` (`person.<key>`, `bot.<key>`). The bridge announces each one with `orchestra.machine.started`, giving the name and team from the owner's identity map. What a system did with no account behind it is the system's own bot (`bot.jira`) | `identity.py`, `announce.py`, `relay.py` |
| **Counted traffic.** What producers put into a broker and consumers took out, without reading a message | A **preset** of series definitions that Orchestra's `[[sources]]` reads from a Prometheus-compatible store. Nothing is published on the bus | `orchestra_bridges/rabbitmq/` (main) |

A bridge is a **relay outside Orchestra** (Orchestra's rule 1: the core knows no tickets or
pings), **never a machine** (Orchestra's rule 2 and its decision D19, "the provider is never the
machine"). The machines are the actors it relays: people and bots in the other system. A bridge
never announces itself, never publishes under its own name (`relay.py` refuses any source but
`person.…` or `bot.…`), is not declared as a machine, and never writes a metric: it serves
`/metrics` for an instrument the plugin declares. What no one did (a gap in what the bridge saw, a
Slack ping running out) is counted, not published. `bridges.<section>` names only a bridge's
process, its log and its instrument. Orchestra counts bridged events by type and source like any
others, so flows, the Machines page, and hand-offs (one machine acting on a subject after another)
come with no extra work.

Every bridge holds to the rules in `AGENTS.md`:

1. **Never text.** It publishes metadata only. Titles, bodies and messages pass through memory and
   are never published, written or logged.
2. **Read only.** It never writes to the system it reads: no comments, labels or reactions.
3. **The token stays in memory.** It is never written, logged or published, and it is sent only
   to the configured API host.
4. **No state of its own.** What it knows comes from the events it relayed (found by their ids)
   and the series its instrument stored (found by `instrument=`), rebuilt at start.
5. **Through Orchestra's library only.** It uses `orchestra.events`, `orchestra.metrics` (to read),
   `orchestra.plugins`, `orchestra.launcher.Process` and `orchestra.collect.spec.Collector`.
6. **A bridge is a relay, never a machine.** Sources are the actors; a system's own act is the
   system's bot (`bot.jira`); what no one did is counted, not published.
7. **Catch up, never assume continuity.** After a gap it reads back to where it left off and
   counts the gap through its instrument.

## 2. Status at a glance

| Piece | Status | Code |
|---|---|---|
| Plugin declaration (processes and instruments; no machines) | Built (main): every configured bridge, and the scrape of each one's `/metrics` | `plugin.py` |
| Relay guard: every source a person or a bot | Built (main) | `relay.py` |
| Gap counts, served by every bridge | Built (main) | `gaps.py` |
| Config parsing (`[bridges]`, `[bridges.github]`, `[bridges.github_prs]`, `[bridges.jira]`, `[bridges.jira_<name>]`, `[bridges.slack]`) | Built (main) | `config.py` |
| Identity map and actors, with namespaced strangers (`person.jira.<login>`, `person.slack.<handle>`) | Built (main) | `identity.py` |
| Shared announcer | Built (main) | `announce.py` |
| Shared `/metrics` server | Built (main) | `serve.py` |
| Shared token reading (environment, or a `chmod 600` file outside the repository) | Built (main): Jira and Slack | `secret.py` |
| GitHub issues bridge, its `/metrics` and its instrument | Built (main) | `github/` |
| GitHub pull-request bridge, its `/metrics` and the instrument the plugin declares | Built (main, merged from `pr-tracking`) | `github_prs/` |
| Jira bridge (Data Center first, and Cloud), its `/metrics`, its instrument, a second site | Built (main, merged from `jira-bridge`) | `jira/` |
| Slack pings bridge, its `/metrics` and its instrument | Built (main, merged from `slack-bridge`) | `slack/` |
| RabbitMQ preset, simulated fleet, Docker rig | Built (main). Orchestra's side (`orchestra/sources/traffic.py`, `preset.py`) is built in Orchestra | `rabbitmq/` |
| Confluence, Google Drive bridges; Bluesky stand-in; Pub/Sub preset; webhooks; team-only mode; write-back (the outbox) | Proposed | none |

## 3. The parts

```mermaid
flowchart LR
    subgraph owner["Owner's files"]
        toml["orchestra.toml<br/>[bridges], [bridges.github],<br/>[bridges.slack], [[sources]]"]
        people["people.toml<br/>(identity map)"]
        tok["~/.config/orchestra-bridges/slack-token<br/>(secret.py) or gh login / GH_TOKEN"]
    end

    subgraph ext["Other systems"]
        gh["GitHub REST API"]
        sl["Slack Web API"]
        rmq["RabbitMQ<br/>rabbitmq_prometheus"]
        vm["VictoriaMetrics<br/>(scrapes RabbitMQ)"]
    end

    subgraph pkg["orchestra-bridges (this repo)"]
        plugin["plugin.py<br/>entry point 'bridges'"]
        main["__main__.py<br/>orchestra bridges, python -m orchestra_bridges"]
        cfg["config.py"]
        ident["identity.py"]
        ann["announce.py"]
        ghb["github/<br/>api, poll, translate,<br/>responses, measures, bridge"]
        slb["slack/<br/>api, search, pings,<br/>waits, bridge"]
        preset["rabbitmq/preset.toml"]
        fleet["rabbitmq/fleet.py + rig/"]
    end

    subgraph orch["Orchestra (~/orchestra_2)"]
        launcher["launcher<br/>(orchestra run)"]
        bus[("event bus<br/>orchestra.#lt;key#gt;.events")]
        store[("metrics store")]
        scrape["instruments bridges.github, bridges.slack<br/>(scrapes the plugin declares)"]
        sources["sources: catalogue,<br/>preset, traffic"]
        ui["Machines and<br/>Instruments pages, map"]
        sd["SD discovery<br/>(the model)"]
    end

    plugin -- "Plugin(processes)" --> launcher
    plugin -- "Plugin(collectors)" --> scrape
    launcher -- "starts, restarts" --> main
    main --> cfg
    cfg -. reads .-> toml
    ident -. reads .-> people
    main --> ghb
    main --> slb
    ghb --> ident
    ghb --> ann
    slb --> ident
    slb --> ann
    ghb -- "GET, read only" --> gh
    slb -- "GET, read only" --> sl
    tok -. "token, in memory" .-> ghb
    tok -. "token, in memory" .-> slb
    ghb -- "events" --> bus
    slb -- "events" --> bus
    scrape -- "GET /metrics" --> ghb
    scrape -- "GET /metrics" --> slb
    scrape -- "instrument=bridges.#lt;section#gt;" --> store
    fleet -- "AMQP" --> rmq
    vm -- "scrape /metrics/detailed" --> rmq
    preset -. "named by [[sources]] preset" .-> sources
    sources -- "PromQL" --> vm
    bus --> ui
    store --> ui
    sources --> ui
    store --> sd
    sources --> sd
    bus --> sd
```

The diagram draws the GitHub issues and Slack bridges; the pull-request (§12) and Jira (§13)
bridges plug in the same way, each with its own instrument (`bridges.github_prs`,
`bridges.jira`), and every bridge shares `relay.py`, `gaps.py`, `serve.py`, `announce.py` and,
for Jira and Slack, `secret.py`. The events go to the bus only through `relay.py`, as the actors.

Two kinds of observation enter Orchestra, and they never mix (Orchestra's rule 3: "Counted traffic
is never turned into events"):

- **Events.** The bridges publish one CloudEvents envelope per occurrence, with a source, a
  subject, a time and an id. They feed the Machines page, the map, hand-offs and event counters.
- **Counts.** The preset defines series that Orchestra's sources read from another store. They
  feed the Machines page (machines "known by counts", double-line edges), counters and backlogs.
  No envelope is ever made from a count.

## 4. How it plugs into Orchestra

### 4.1 The Plugin declaration

`pyproject.toml` registers the entry point:

```toml
[project.entry-points."orchestra.plugins"]
bridges = "orchestra_bridges.plugin:plugin"
```

Orchestra calls `plugin(project)` and gets an `orchestra.plugins.Plugin`:

| Field | Value |
|---|---|
| `name` | `"bridges"` |
| `machines` | none: a bridge is a relay, and the machines are the actors it publishes as, known once seen |
| `processes` | one `Process(s, [sys.executable, "-m", "orchestra_bridges", s], stop_grace_s=10)` per configured section: `SYSTEMS = ("github", "github_prs", "slack")`, then `jira` and any `jira_<name>` |
| `collectors` | one `Collector.scrape(s, "http://<host>:<port>/metrics", every_s=30)` per configured bridge (default ports, `PORTS`: `github` 9464, `slack` 9465, `github_prs` 9466, `jira` 9467; any `jira_<name>` must set its own) whose `port` is fixed (not 0); a wildcard host is scraped on loopback (§4.3) |
| pages, router, scripts | none |

The plugin has no pages. What a bridge shows is Orchestra's own Machines and Instruments pages,
map and series. Until 2026-10-27 the plugin declared each bridge as a machine (a placeholder,
`Declared("bridges.<s>", "program", "continuous")`) and each bridge announced itself; under D19 a
bridge is infrastructure, so neither happens. A bus from before still holds those announcements
and events, so Orchestra lists `bridges.<section>` there from them; a new bus never shows it.

### 4.2 Processes

The launcher (`orchestra/launcher.py`) names a plugin's processes `<plugin>.<name>`. The process
`github` is therefore **`bridges.github`** in the runtime record, and its log is
`<state>/logs/machines/bridges.github.log`. The launcher:

- runs it in the repository root, in its own process group, with `ORCHESTRA_REPO` (the root) and
  `ORCHESTRA_MACHINE_LOG` (its log) set;
- restarts it when it exits, with a back-off that doubles up to a minute, and gives up after
  repeated quick failures (`orchestra.process.gave_up`);
- stops it with SIGTERM, then SIGKILL after `stop_grace_s = 10`;
- reads `orchestra.toml` again when it changes and asks the plugin for its processes again. Adding
  `[bridges.github]` starts the bridge without restarting the host; removing it stops the bridge;
- restarts it when its section or `[bridges] people` changes (`Process.config_keys`): the bridge
  reads its settings once, when it starts, and its command never changes. A bridge given up on is
  tried again then too, so fixing its settings is enough.

`__main__.py` loads the project from `ORCHESTRA_REPO`, builds the bridge, and runs it until
SIGTERM or SIGINT. A configuration, identity or credential error prints one line and exits 2. To
poll once by hand:

```sh
uv run orchestra --repo /path/to/project bridges github --once
```

### 4.3 The instruments that store what the bridges serve (Slack's as the example)

No bridge writes a metric (D19). Each serves its counts at `/metrics` (§5.6, §7.6), and an
**instrument** stores them. Since Orchestra `8344264c` a plugin can declare instruments
(`Plugin.collectors`, Orchestra's PLUGINS.md §4.7), so `plugin.py` declares the scrape itself
whenever `[bridges.slack]` exists (and likewise for every other bridge):

```python
Collector.scrape("slack", "http://127.0.0.1:9465/metrics", every_s=30)   # host and port from [bridges.slack]
```

Orchestra runs it as **`bridges.slack`** (`<plugin>.<name>`), lists it on the Instruments page,
not the Machines page, and logs it to `<state>/logs/instruments/bridges.slack.log`. A wildcard
`host` (`0.0.0.0`, `::`) is scraped on the loopback address. With `port = 0` (any free port) there
is no fixed address to scrape, so the plugin declares none.

Every point the instrument stores is labelled `instrument="bridges.slack"` and `project`, and keeps
the bridge's own `workspace`. On start, the Slack bridge reads its counters and its last good poll
back by `instrument="bridges.slack"` and its workspace. The instrument and the process share the
name `bridges.slack` but are two different things, and neither is a machine: the process relays
the actors' acts, the instrument measures the system, and the label is provenance.

**Before 2026-10-27** a plugin could not declare a scrape, so the owner added one by hand:

```toml
[[scrape]]
name = "bridges.slack"            # must be this name
url = "http://127.0.0.1:9465/metrics"
every = "30s"
```

That entry still works: a project entry with the same name replaces the plugin's (Orchestra logs
`plugin bridges: its collector bridges.slack is replaced by the project's entry of that name`). Keep
it only to scrape more often or elsewhere. It must keep the name `bridges.slack`, or a restart
would not find the bridge's counts. Back then the scrape labelled points `machine="bridges.slack"`;
now it labels them `instrument="bridges.slack"`, so each series starts again under the new label at
the upgrade. The bridge reads both labels on start and continues each counter from the higher
value, so no count is lost or taken down, and the gap test still finds the last good poll.

### 4.4 What Orchestra reads, and what it does not

Orchestra reads each event's envelope: `type`, `source`, `subject`, `time` and `id`. It never
reads `data` (Orchestra's rule 3). The one exception is `orchestra.machine.started`. From it,
Orchestra reads `mode`, `kind`, `listens_for`, `log` and `package`, plus `name`, and `team` as the
machine's group (`orchestra/web/machines.py`). The rest of the announcement goes to the package's
card unread.

---

## 5. The GitHub issues bridge (main)

### 5.1 Modules

| Module | Responsibility |
|---|---|
| `github/api.py` | Read-only REST client. Follows `Link: rel="next"` pages (`per_page=100`). Honours `Retry-After` and the primary rate limit. Sends the token only to the configured scheme and host |
| `github/poll.py` | One read of a repository since a moment `t`: three paged lists, de-duplicated by id and sorted by (time, rank, id) |
| `github/translate.py` | API objects become `Seen` records: type, stable id, GitHub's time, metadata-only data. Pull requests are dropped |
| `github/responses.py` | First response: which issues wait, and how long answered ones waited. Counters that never go down |
| `github/measures.py` | Serves those counts and the gap counts on `/metrics`, each point at the time of its change, never before the last point served; a scrape takes what waits |
| `github/bridge.py` | Resume, announce the actors, poll, publish, count gaps, `/metrics`, the loop |
| `identity.py` | Login to actor (`person.<key>`/`bot.<key>`), with the identity map |
| `config.py` | `[bridges]` and `[bridges.github]` |

### 5.2 Polling

`Bridge.run` starts `/metrics` (503 until resumed), calls `resume()`, then `reannounce()` (people
whose name or team changed in the map), and then loops: `round()`, then `stop.wait(every)`. A round polls each repository in turn. A `GitHubError`
fails only that repository's poll: it is logged, nothing is published for it, and the next round
tries again.

One poll of a repository (`poll.since(api, repo, t)`) reads three lists:

| List | Query | Becomes |
|---|---|---|
| `/repos/<repo>/issues` | `state=all&sort=updated&direction=asc&since=<t>` | `github.issue.opened` for issues **created** at or after `t` |
| `/repos/<repo>/issues/comments` | `sort=updated&direction=asc&since=<t>` | `github.issue.commented` for comments **created** at or after `t`. An old comment that was edited is not new |
| `/repos/<repo>/issues/events` | none; newest first, paged until a page ends older than `t` | `github.issue.labelled`, `.closed` and `.reopened`; other event kinds are ignored |

Pull requests appear in all three lists and are left out: an issue with a `pull_request` key, or a
comment whose `html_url` contains `/pull/`.

`t` is `max(since, now - max_catchup)`. After each good poll, `since = now - overlap`, so every
poll reads the last `overlap` (10 minutes by default) again. That catches items GitHub shows late.

### 5.3 Stable ids from the system, so polls never duplicate

Every event's id is built from GitHub's own id for that occurrence (`translate.py`):

| Event | Id |
|---|---|
| `github.issue.opened` | `github:<owner>/<repo>:opened:<issue number>` |
| `github.issue.commented` | `github:<owner>/<repo>:comment:<comment id>` |
| `github.issue.labelled`, `.closed`, `.reopened` | `github:<owner>/<repo>:event:<issue event id>` |
| actor announcement | `github:announce:<source>:<first 12 hex of sha1(announcement)>:<time>` |

The `time` is GitHub's (`created_at`, normalised to RFC 3339 with `+00:00`). The bridge keeps a
`seen` set of every id it has published. A poll that overlaps the last one, or a restart that reads
the same window again, finds those ids in `seen` and publishes nothing twice. `Events.publish(...,
time=, id=)` puts both into the CloudEvents envelope as given.

The `seen` set is in memory only. It is rebuilt from the bus at each start (§5.4) and grows as long
as the process runs: a few thousand ids a day for a busy repository.

### 5.4 Resuming from the bus, with no state of its own

The bridge writes no file. At start, `Bridge.resume()` does the following:

1. Reads the whole bus (`events.after(0)`) and keeps the events whose id starts with `github:`.
2. Sets `seen` to those ids. It also rebuilds each actor's last announcement from them, so it knows
   who was already announced and with what.
3. For each repository, it **replays** that repository's issue events (`github:<repo>:*`) in time
   order through a fresh `Responses`. Events published before the `github.` prefix
   (`issue.opened`, …) are still read, through `translate.current`.
4. Reads the store (`Measures.resume`): the latest point of
   `github_issues_unanswered{instrument="bridges.github",repo}` becomes the **watermark** (when the
   last poll's points were stamped), and each counter's latest value is kept. Points the bridge
   wrote itself before 2026-10-27 (`machine="bridges.github"`) are read too, and the later time and
   higher value taken. The gap counters continue from their stored values the same way.
   `Responses.continue_from` then adds an offset to each counter, so it continues from its last
   written value. A trimmed bus, or events folded in another order, can therefore never take a
   counter down, which discovery would read as a reset.
5. Sets `last_ok` to the watermark, and `since` to the later of the last event and the watermark,
   less `overlap`. With nothing on the bus and nothing in the store, `since = now - lookback`.

If the store cannot be read, the failure is logged and the bridge carries on with a zero
watermark: measuring never stops the bridge.

### 5.5 Catching up after the laptop sleeps, and gap counts

Orchestra runs on the owner's laptop, which sleeps. The bridge never assumes it was running
continuously (rule 7, and the owner's answer 3 in the proposal).

- **Gap test.** If the last good poll of a repository began more than `GAP_ROUNDS = 3` rounds ago
  (`now - last_ok > 3 × every`), this poll closes a gap.
- **Catch-up.** The poll reads from `since`, but never from earlier than `now - max_catchup`
  (48 hours by default).
- **Gap count.** Once the reads succeed, it counts the gap (`gaps.py`), never publishes it: a gap
  is no one's act. `github_issues_bridge_gaps_total{repo}` goes up by one,
  `github_issues_bridge_gap_seconds_total` by `now - last_ok`, and
  `github_issues_bridge_gaps_recovered_total` by one unless `since` was older than the
  `max_catchup` floor (part of the gap was not read). Failed polls in between count nothing, so
  one outage is one gap. (Until 2026-10-27 this was a `bridge.gap` event from `bridges.github`;
  nothing read it.)
- **True times.** Events missed in the gap are published with GitHub's times. Points for the
  changes they cause are served at those times too, because nothing was served during the gap:
  each is stamped at `max(t, watermark)`, and one scrape takes them all.

```mermaid
sequenceDiagram
    autonumber
    participant L as Launcher (orchestra run)
    participant B as bridges.github (process)
    participant Bus as Orchestra bus
    participant I as instrument bridges.github
    participant S as Metrics store
    participant G as GitHub API

    Note over B: Running normally: one poll every `every` (60 s)
    B->>G: issues, comments, events since (last poll − overlap)
    G-->>B: items
    B->>Bus: new events as their actors (ids github:…, GitHub's times)
    Note over B: points at now wait for the scrape (watermark W)
    I->>B: GET /metrics (every 30 s)
    B-->>I: every point waiting, each with its time
    I->>S: points labelled instrument=bridges.github

    Note over B,G: Laptop sleeps. The process is suspended or killed.
    L->>B: (if it exited) start again, with back-off
    B->>Bus: after(0)
    Bus-->>B: own events (ids github:…)
    Note over B: seen = their ids. Replay them in time order to rebuild the counts
    B->>S: latest(github_issues_unanswered), latest(each counter), by instrument=bridges.github
    S-->>B: watermark W, last counter values
    Note over B: counters continue from stored values. last_ok = W. since = max(last event, W) − overlap
    B->>Bus: re-announce map-listed people whose name or team changed

    Note over B: Next round: now − W > 3 × every, so this poll closes a gap
    B->>G: read from max(since, now − max_catchup)
    alt reads fail (still offline, 5xx, rate limit too long)
        G--xB: error
        Note over B: log it, publish nothing, try again next round
    else reads succeed
        G-->>B: everything since the window start
        Note over B: count the gap: gaps_total +1, gap_seconds_total + (now − W)
        loop each item whose id is not in seen
            B->>Bus: orchestra.machine.started for the actor (if new or changed)
            B->>Bus: github.issue.* with GitHub's id and time, as the actor
        end
        Note over B: count changes older than overlap, stamped at their GitHub times (≥ W); all series again at now
        I->>B: GET /metrics
        B-->>I: every point waiting
        I->>S: points at their own times
        Note over B: last_ok = now. since = now − overlap
    end
```

**What the restart does not see.** If the store holds no point for a repository (a first start,
or a store that never took a point), `last_ok` stays unset after a restart, and the first poll
counts no gap. It still reads back from the last event on the bus.

### 5.6 First-response counters

`responses.py` measures how long issues wait for someone other than their author. This is the
GitHub stand-in for "I am pinged and must answer within 15 minutes".

- An issue **waits** from its `github.issue.opened`, but only if the bridge saw it opened. Issues
  opened before the bridge first watched are not counted.
- It is **answered** by the first `github.issue.commented` or `github.issue.closed` whose source is
  not the author. A source starting `bot.` answers only with `bots_answer = true`. A **label never
  answers**, because triage bots label everything.
- It **closes unanswered** when the author, or a bot that does not count, closes it first.
- A reopened issue does not wait again.

Counting happens in time order. An event is published as soon as it is read, but folded into the
counts only once it is older than `overlap`. Until then it stays in `pending`, because a later
poll may still read an earlier event (a comment GitHub stamps before a close the bridge already
read). A restart's replay counts the same way.

The series, served at `/metrics` with `repo="<owner>/<repo>"` and stored by the instrument
`bridges.github`, which adds `instrument="bridges.github"` and `project`. The label is provenance
and how the bridge finds its series on restart; Orchestra never associates a flow with a machine
from it. (Until 2026-10-27 the bridge wrote these series itself, labelled
`machine="bridges.github"`; the names are unchanged.)

| Series | Kind | Meaning |
|---|---|---|
| `github_issues_unanswered` | level | Issues waiting now: **the stock** |
| `github_issues_unanswered_oldest_seconds` | level | How long the oldest has waited |
| `github_issues_opened_total` | counter | Inflow: issues opened while watched |
| `github_issues_answered_total{within="15m\|1h\|1d\|over"}` | counter | Outflow, by wait: ≤15 min, ≤1 h, ≤1 day, longer. Buckets do not overlap |
| `github_issues_closed_unanswered_total` | counter | Outflow: closed before anyone answered |
| `github_issue_first_response_seconds_total` | counter | Sum of answered waits. Mean wait = its rate ÷ the answered rate |
| `github_issues_bridge_gaps_total`, `…_gaps_recovered_total`, `…_gap_seconds_total` | counter | Gaps in what the bridge saw (§5.5) |

All series are written together at each change, so they balance exactly:

```
unanswered = opened − Σ answered(within) − closed_unanswered
```

The response-time distribution is a set of counters by bucket because Orchestra's collectors keep
no histogram. `measures.py` serves every series once per poll at `now`, even when nothing changed,
so a quiet repository can be told apart from a stopped bridge. It never serves a point earlier
than the last one for that repository. The store reads a series as its last value at or before
each instant, so a point slipped in behind a later one would change what was already read. A
change GitHub reports late is therefore stamped up to one poll late.

**Points wait for the scrape.** Each point is kept in memory, with its own time, until a
`GET /metrics` takes it: the answer holds every point waiting, several of one series at their own
times (the sample's timestamp in milliseconds). Orchestra keeps a sample's timestamp and keeps one
point per series per timestamp, so a gap's catch-up reaches the store point by point. A scrape that
fails after reading loses those points, never events; the next one's totals are whole. At most
50,000 points wait (the oldest go first).

### 5.7 The identity map

`identity.py` turns an account into the machine an event's source names. The map is a TOML file
named by `[bridges] people` (relative to the repository). It is optional; when it is missing,
nobody is listed.

```toml
[people.daniel]
name = "Daniel Lewis"
team = "sre"
github = "danroblewis"            # one account, or a list

[people.triage]
name = "VS Code triage bot"
team = "vscode"
bot = true
github = "vscode-triage-bot"
```

| Account | Source |
|---|---|
| Listed in the map | `person.<key>`, or `bot.<key>` when `bot = true`, with `name` and `team` |
| Not listed, and GitHub says it is a bot (`type: Bot`, or a login ending `[bot]`, which is dropped) | `bot.<login>` |
| Not listed | `person.<login>` |
| Deleted account (no login) | `person.ghost` |

Sources are lowercased. Any character other than `a-z 0-9 . _ -` becomes `-`. Every key other than
`name`, `team` and `bot` in an entry names a system (`github`, `jira`, `slack`). Through
those keys, the map joins one person's accounts into one machine. People are shown by name, not as
pseudonyms: this follows the owner's answer 6 and replaces the proposal's salted-hash default.

### 5.8 Announcements

Orchestra learns a machine's kind and group from `orchestra.machine.started` (Orchestra's rule 2).
The bridge announces only the actors, never itself: each actor just before the actor's first
event, with that event's time and the id `github:announce:<source>:<hash>:<time>`, through the
shared `announce.Announcer`. The data is `Actor.announcement("github")`: `{kind: "person" |
"program", mode: "ad hoc", package: "bridges", github: <login>, bot, name?, team?}`.

An actor is announced again whenever the announcement would differ from their last one on the bus,
for example when the map gave them a new team. This happens before their next event. It also
happens at start, through `reannounce()`, for everyone the map lists on GitHub who was announced
before. A team changed in the map therefore shows even if that person does nothing. Someone the map
lists but the bridge never saw is not announced.

### 5.9 Credentials and the API

`api.gh_token(api)` finds the token the way `gh` does:

| API host | Token, in order |
|---|---|
| `api.github.com` | `GH_TOKEN`, `GITHUB_TOKEN`, then `gh auth token --hostname github.com` |
| Anything else (GitHub Enterprise, or a fake) | `GH_ENTERPRISE_TOKEN`, `GITHUB_ENTERPRISE_TOKEN`, then `gh auth token --hostname <host>` |

An Enterprise API therefore never receives the github.com token. The token is read once at start
and held only by the `GitHub` object, whose `repr` hides it. It goes in the `Authorization: Bearer`
header only to the configured API's own scheme and host. A pagination link to another origin
raises before the request is sent. A redirect to another origin is refused by a custom redirect
handler; by default, urllib would copy the header to the new host.

Rate limits:

- A 403 or 429 with `Retry-After` waits that long, capped at `MAX_WAIT_S = 900` seconds.
- A primary limit that has run out (`X-RateLimit-Remaining: 0`) waits until `X-RateLimit-Reset`
  if that is within 900 seconds. Otherwise the poll fails.
- A 429 without headers waits 60 seconds.
- Each request is tried at most `RETRIES + 1 = 4` times.
- Errors give the path and the HTTP code, never a body.

### 5.10 Limits

The README states these limits, and the code confirms them:

- Issues opened before the bridge first watched are never counted as waiting.
- How far back GitHub will page a busy repository's issue events is GitHub's limit.
  `max_catchup` bounds only what the bridge asks for.
- `every` is the pause *between* rounds, so the real period is `every` plus the time a round
  takes.
- Every distinct actor in a busy repository becomes a machine: hundreds of them.

---

## 6. Counted traffic: the RabbitMQ preset and rig (main)

Some buses carry too much to read, or data that is not ours. Orchestra reads them by **counts**:
what each producer put into a channel, what each consumer took out, and what waits. This part of
the repository is **configuration and a test system, not a bridge**. It publishes nothing on the
bus. Orchestra's side is built in Orchestra (`orchestra/sources/traffic.py`, `preset.py`,
`catalogue.py`, and `web/traffic.py`; Orchestra SPEC §4.4). Presets ship with whatever reads that
store, never with Orchestra (decision D10).

### 6.1 The preset (`rabbitmq/preset.toml`)

A project names the preset from a `[[sources]]` table:

```toml
[[sources]]
name = "rabbit"
url = "http://127.0.0.1:8441"         # the Prometheus-compatible store that scrapes RabbitMQ
preset = "~/orchestra-bridges/orchestra_bridges/rabbitmq/preset.toml"
settle = "30s"
[sources.producers]                   # exchange = producer: RabbitMQ cannot say who publishes
orders = "orders-api"
```

Orchestra's `sources/preset.py` merges the preset's `[source]` defaults under the table's own
keys, and adds its `[[series]]`, each reading this source. The preset sets `infer = false`, so
Orchestra reads only these definitions and never infers from the store's schema. It also sets
`machine = "consumer"`, so a `consumer` label names a machine.

| Series | How it is read | Kind | `traffic` |
|---|---|---|---|
| `rabbitmq_published_total` | `select` of `rabbitmq_detailed_channel_messages_published_total` (not the default exchange, not `amq.*`), `by = ["vhost", "exchange"]`. Raw samples, so a channel that closes and reopens is no reset | counter | `out`, channel `{exchange}`, no machine: unattributed unless `[sources.producers]` names one |
| `rabbitmq_acked_total` | PromQL: per-queue acks, joined to the queue's exchange through `queue_exchange_messages_published_total` (`group_left`), with `consumer` taken from the queue name `<consumer>.<anything>` by `label_replace` | counter | `in`, channel `{exchange}`, machine `{consumer}` |
| `rabbitmq_backlog` | PromQL: `rabbitmq_detailed_queue_messages` (ready + unacked), with the same join and the same consumer rule | level | `backlog`, channel `{exchange}`, machine `{consumer}` |
| `rabbitmq_consumers` | PromQL: `queue_consumers` per queue, with the consumer rule | level | none |

**Never a message.** RabbitMQ's own exporter counts. Nothing reads a payload, a header or a
routing key. RabbitMQ must expose `/metrics/detailed` from `rabbitmq_prometheus` with the families
`queue_coarse_metrics`, `queue_consumer_count`, `queue_delivery_metrics`, `queue_exchange_metrics`
and `channel_exchange_metrics` (`rig/scrape.yml`).

**Limits stated in the preset.**

- A queue must be bound to exactly one exchange, or the join fails.
- Acks are an evaluated sum, not raw samples. A queue deleted and declared again restarts at 0,
  the drop reads as a counter reset, and that step's acks are lost.
- RabbitMQ names channels by process id, never by publisher. Producers are therefore unattributed
  unless declared.

### 6.2 How counted traffic reaches the model

```mermaid
flowchart LR
    subgraph fleet["fleet.py (7 services)"]
        oa["orders-api"]
        pa["payments-api"]
        web["web"]
        bil["billing<br/>(acks in batches every 20 s)"]
        shp["shipping"]
        ntf["notifier"]
        ana["analytics<br/>(too slow at peak)"]
    end

    subgraph rabbit["RabbitMQ"]
        xo{{"exchange orders"}}
        xp{{"exchange payments"}}
        xc{{"exchange clicks"}}
        q1[("billing.orders")]
        q2[("shipping.orders")]
        q3[("analytics.orders")]
        q4[("billing.payments")]
        q5[("notifier.payments")]
        q6[("analytics.clicks")]
        exp["rabbitmq_prometheus<br/>/metrics/detailed<br/>(counts only)"]
    end

    oa --> xo
    pa --> xp
    web --> xc
    xo --> q1 & q2 & q3
    xp --> q4 & q5
    xc --> q6
    q1 & q4 --> bil
    q2 --> shp
    q5 --> ntf
    q3 & q6 --> ana

    exp -- "scraped every 5 s" --> vmx[("VictoriaMetrics :8441")]

    subgraph orch["Orchestra"]
        src["[[sources]] rabbit<br/>preset.toml, infer = false,<br/>[sources.producers] orders = orders-api"]
        tr["sources/traffic.py<br/>resolve channel, machine"]
        cat["Catalogue<br/>series rabbit:rabbitmq_*"]
        mp["Machines page and map<br/>machines rabbit.billing, …<br/>channels rabbit:orders, …<br/>double lines with rates;<br/>unattributed stubs"]
        disc["SD discovery"]
        model["Model:<br/>stock = rabbitmq_backlog per queue<br/>inflow = publishes into its exchange (a copy into each bound queue)<br/>outflow = rabbitmq_acked_total of its consumer"]
    end

    vmx -- "PromQL over the window" --> src
    src --> tr --> cat
    cat --> mp
    cat --> disc --> model
```

Each series becomes `rabbit:<name>`. Its machine becomes `rabbit.<consumer>`, or
`rabbit.orders-api` for the declared producer, and that is the series' `machine` label, so
discovery attributes the flow to that machine. Its channel becomes `rabbit:<exchange>`. In the rig,
discovery finds each slow or batched consumer's backlog (billing's and analytics') as a stock,
filled by its exchange's publishes and drained by its own acks. A fanout exchange's publishes are
one flow, with a copy into each backlog it fills. A consumer that keeps up (shipping, notifier)
leaves a backlog too small to be a stock. The README describes this result. The model-acceptance
workflow judges it, not the unit tests.

`tests/unit/test_rabbitmq_preset.py` loads the preset through Orchestra's own `Catalogue`, against
a fake store of real `rabbitmq_detailed_*` shapes (`tests/fake_rabbit_store.py`). It checks that
each series counts the right thing, in the right direction, on the right channel, for the right
machine. `tests/contract/test_rabbitmq_live.py` runs against the live rig.

### 6.3 The rig

`rabbitmq/rig/rig.sh up|down|status` (Docker):

| Part | Container or process | Ports (127.0.0.1 only) |
|---|---|---|
| RabbitMQ 4.0.3, with management and prometheus, `collect_statistics_interval = 1000` | `orchestra-test-rabbit` | AMQP 5682, management 15682, metrics 15702 |
| VictoriaMetrics v1.106.1, scraping every 5 s (`scrape.yml`), 7-day retention | `orchestra-test-rabbit-vm` | 8441 |
| The fleet (`fleet.py`, `uv run --no-project --with pika`) | pid and log in `~/.local/state/orchestra-rabbit-rig/` | none |

`fleet.py` runs seven processes. Each has its own connection, named after its service. Demand
follows a "day" compressed into `--period` (20 minutes): `mean × (1 + 0.7 sin 2πt/period)`. Orders
publish about 4 per second, payments about 2, and clicks about 6. At peak, about 17 per second reach
analytics, which handles about 14. Queues are named `<consumer>.<what>`, the naming the preset
reads consumers from. `rig/orchestra.toml` is an example project for the rig: window 40 minutes,
step 15 seconds, conservation strategy, and `settle = "30s"`, because VictoriaMetrics hides the
newest 30 seconds from range queries.

---

## 7. The Slack pings bridge

> **Built (main)**, merged from `slack-bridge` (`aae18aa`). It uses the shared `secret.py`,
> `serve.py`, `announce.py` and `identity.py` like the other bridges.

The need comes from the proposal: "someone will @ ping me on Slack and be mad if I don't respond
within 15 minutes". The bridge measures that promise as a stock.

### 7.1 Modules

| Module | Responsibility |
|---|---|
| `slack/api.py` | Read-only Web API client for `auth.test`, `search.messages` and `reactions.list`. Same-origin token. `Retry-After` handling bounded by one poll |
| `slack/search.py` | The searches, newest first, until a match is older than the window. Reads a match's text in memory for mentions, then drops it |
| `slack/pings.py` | What a ping is. Event types, ids and subjects |
| `slack/waits.py` | The waiting pings and the counters, rendered as Prometheus text |
| `slack/bridge.py` | Identify, resume, read, publish as the actors, count gaps, the loop |
| `secret.py` | Finds the user token (§7.9), shared with the Jira bridge |
| `serve.py` | `/metrics` HTTP server for the instrument, shared |
| `announce.py` | The shared announcer |

### 7.2 Pings

A **ping** (`pings.kind`) is a message from someone other than the owner that is one of these:

- `dm`: in a DM channel (`channel_type == "im"`), when `dms = true`;
- `mention`: its text contains `<@owner>`;
- `group`: its text contains `<!subteam^S…>` for a configured group.

A message without a member id (old-style bot posts, webhooks) is not a ping. `@here` and
`@channel` are not pings.

Each poll first identifies the owner with `auth.test`. This happens once at start, retried each
round until it succeeds. The poll then runs these searches, each with `after:<UTC date two days
before the window>`:

- `<@me>`;
- `<!subteam^G>` for each group;
- `to:<@me>` when `dms`;
- `from:<@me>`, only for the reply rule.

`queries` in the config replaces the first three, with `{me}` filled in. Each search is read
newest first, 100 a page, until a match is older than the window or the pages run out (at most
100). The two-day `after:` covers every time zone from UTC−12 to UTC+14. The exact cut is each
match's own `ts`.

The subject is the thread, `<workspace>/<channel>/<thread ts>`, where the thread ts is the parent's
ts, or the message's own when it is not a reply. A ping and its answer share the subject, so
Orchestra sees the hand-off from the pinger to the owner.

### 7.3 The checkmark and reply rules

`answered` (default `["reaction"]`, never empty) picks the rules:

- **Reaction rule (the default).** This follows the owner's answer 5: "Answered usually means a
  checkmark emoji reaction". A reaction named in `reactions` (default `white_check_mark`,
  `heavy_check_mark`; skin tones ignored), put on a waiting ping by the owner, answers the ping. It
  also answers the earlier waiting pings that a reply in the same place would answer:
  - for a threaded ping, the earlier pings in that thread;
  - for an unthreaded ping, the earlier unthreaded pings in that channel.

  So a checkmark on the last of five DM lines answers all five. Slack gives no time for a
  reaction, so the answer is stamped **when the bridge first sees it**, up to one poll late. Its
  data says `observed: true` and `not_before`, the last moment the ping could have been seen
  unanswered. If `not_before` is more than `KNOWN_ROUNDS = 2` polls back (after a gap or on a
  first start), the wait is not known. It is counted as `within="unknown"` and left out of the
  summed waits.
- **Reply rule.** The owner's first message after the ping answers it, at that message's own time:
  in the ping's thread, or, for an unthreaded ping, the next unthreaded message in the same
  channel.

With both rules on, the poll applies replies before reactions. **When both see an answer in the
same poll, the reply wins**, because it has a real time. Under the reaction rule alone, a ping
answered only by a reply is never answered: it expires.

### 7.4 `reactions.list`, not one call per ping

The checkmarks come from **one read of `reactions.list`**. That read lists the owner's own
reactions, most recently reacted first, with `full=true`, 100 a page, for at most
`REACTION_PAGES = 5` pages. It is not one `reactions.get` per waiting ping, so the cost does not
grow with the number of pings waiting. It runs only when the reaction rule is on and some ping
waits or is new.

**Reactions never block a poll.** Only the searches fail a poll. If `reactions.list` fails (a rate
limit, the network, or a token without `reactions:read`), the bridge does the following:

- logs it once;
- keeps whatever the earlier pages found;
- leaves the other pings waiting for the next poll;
- publishes everything else it read.

A checkmark is found only while it is among the owner's 500 most recent reactions. After a long
gap, a ping with more than 500 later reactions above its checkmark is not seen as answered, and it
expires.

### 7.5 Expiry

A ping that has waited `expire` (default `168h`, 7 days) stops waiting, at `t0 + expire`. No one
did that, so nothing is published: `slack_pings_expired_total` counts it (`Waits.expire`). Until
2026-10-27 the bridge published `slack.ping.expired` as itself; nothing else read it, and such
events on a bus are still read back by their ids. On restart the replay expires, before each
event, every ping whose `expire` had run out by then, as the polls did. A deleted ping is not noticed, so it too waits until it expires. A reply after the
expiry does not answer. Within one poll, expiries are applied before reactions. A checkmark first
seen on the poll where the ping expires therefore comes too late.

So the stock balances exactly, as long as the bus keeps the bridge's events (and `expire` is not
changed between runs; a counter still never goes down if it is):

```
unanswered = received − Σ answered(within) − expired
```

### 7.6 `/metrics`, and the 15-minute promise as a stock

Like every bridge, the Slack bridge **serves** its counts rather than writing them.
`serve.py` listens on `host:port` (default `127.0.0.1:9465`). `GET /metrics` returns Prometheus
text, or 503 until `resume()` has finished. Other paths get 404. Requests are not logged. Orchestra
reads it through the instrument `bridges.slack` the plugin declares (§4.3), which adds
`instrument="bridges.slack"` and `project`. Every series carries `workspace="<team id>"`.

| Series | Kind | Meaning |
|---|---|---|
| `slack_pings_unanswered` | gauge | Pings waiting now: **the stock** |
| `slack_pings_oldest_unanswered_seconds` | gauge | How long the oldest has waited |
| `slack_pings_received_total` | counter | Inflow |
| `slack_pings_answered_total{within="5m\|15m\|60m\|over\|unknown"}` | counter | Outflow by wait. The buckets do not overlap: `15m` is more than 5 and at most 15 minutes |
| `slack_pings_expired_total` | counter | Outflow: expired |
| `slack_ping_wait_seconds_total` | counter | Sum of waits in the timed buckets. `unknown` is excluded |
| `slack_bridge_last_poll_timestamp_seconds` | gauge | Unix start time of the last successful poll. Absent until the first |
| `slack_bridge_gaps_total`, `…_gaps_recovered_total`, `…_gap_seconds_total` | counter | Gaps in what the bridge saw (§7.8) |

**The 15-minute promise**, as the proposal defines it (§5):

- The share answered in time is `(5m + 15m) / (5m + 15m + 60m + over)`, with `unknown` left out.
- The oldest age (`slack_pings_oldest_unanswered_seconds`) should stay under 900.
- The mean wait is `rate(slack_ping_wait_seconds_total)` ÷ the rate of timed answers, or by
  Little's law, the stock ÷ the answer rate.

Inflow and outflow are also Orchestra's own counts of `slack.ping.received` and
`slack.ping.answered`, a balance discovery should find. Waits run on the wall clock, nights and
weekends included. Working hours are an open question for the owner.

The cost of serving without timestamps (the GitHub issues bridge stamps its points, §5.6): the
counts are stamped at the scrape, not at Slack's times. A catch-up after a gap moves the counters when the bridge wakes. The events keep the true
times.

### 7.7 The ping lifecycle

```mermaid
stateDiagram-v2
    [*] --> Waiting: search finds a ping (DM, @me, @group) not in seen<br/>publish slack.ping.received<br/>source = pinger, time = message ts

    Waiting --> Waiting: reactions.list failed<br/>(logged, retried next poll)
    Waiting --> AnsweredReply: reply rule — owner's first later message<br/>in the thread (or unthreaded in the channel)<br/>stamped at the reply's ts
    Waiting --> Expired: t0 + expire ≤ now (checked before reactions)<br/>no event: counted in slack_pings_expired_total
    Waiting --> AnsweredCheck: reaction rule — owner's checkmark on this ping<br/>or on a later ping in the same thread or unthreaded channel<br/>stamped when first seen

    state AnsweredCheck {
        [*] --> Timed: now − not_before ≤ 2 polls
        [*] --> Unknown: now − not_before > 2 polls<br/>(after a gap or a first start)
    }

    AnsweredReply --> [*]: slack.ping.answered<br/>source = owner, rule = reply, wait_seconds, within
    AnsweredCheck --> [*]: slack.ping.answered<br/>source = owner, rule = reaction, observed, not_before
    Expired --> [*]
```

A deleted ping simply stays `Waiting` until it expires.

### 7.8 Resume and gaps

This works as in §5.4–5.5, with three differences:

- **Where it left off comes from a served value, not a point's time.** A scrape keeps storing the
  served counts while polls fail, so the time of the latest point does not mark the last good
  poll. On start, the bridge reads the *value* of `slack_bridge_last_poll_timestamp_seconds`
  instead. That value becomes `last_ok`, and `since` is the later of it and the last event, less
  `overlap`. Without the scrape, a restart reads from its last event and counts no gap.
- **Counters continue from the larger of the rebuilt and the stored values.** A trimmed bus does
  not take them down, and a count the scrape had not yet stored is not lost. The GitHub issues
  bridge instead continues from the stored value, whose points it stamped itself.
- **It counts at once, with no `overlap` delay**, and measures the gap from the *end* of the last
  good poll, so a poll slowed by a `Retry-After` is not taken for a gap. The gap is counted by
  workspace (`slack_bridge_gaps_total` and the rest), never published.

Ids on this bridge are `slack:<workspace>:<ping|answered>:<channel>:<ping ts>` (and `expired`, on
events from before 2026-10-27). Note that
the id uses the **ping's** ts, even for an answer. On resume, events are replayed in time order. An
answer whose `within` was `unknown` is replayed as unknown.

### 7.9 Token rules

`secret.read_token("SLACK_USER_TOKEN", "slack-token", …)` looks for the token in this order:

1. `SLACK_USER_TOKEN`;
2. the file named by `SLACK_USER_TOKEN_FILE`;
3. `[bridges.slack] token_file`;
4. `~/.config/orchestra-bridges/slack-token`.

A token file is refused if it is inside the repository, if it is readable or writable by group or
others (it must be `chmod 600`), or if it is empty. `config.py` refuses a `token` key in
`[bridges.slack]` outright. `api` must be `https` on `slack.com` or a host under it, or a loopback
host for a local fake. Anything else is refused, because the token reads every one of the owner's
messages. Redirects to another origin are refused. A `Retry-After` longer than one poll (`max_wait
= every`) fails the call instead of waiting. The wait uses the stop event, so SIGTERM ends it.
Errors name the method and Slack's error code, never the token, the body or the query.

The README warns against setting `SLACK_USER_TOKEN` where `orchestra run` runs: the launcher passes
its environment to every process it starts, so every machine would get the token. Use the file.

It needs a **user token** (`xoxp-`) with `search:read`, and `reactions:read` for the checkmark rule.
A bot token cannot search.

### 7.10 The privacy rule: never text

`search.messages` returns each match's text. `search.message()` reads it once, in memory, to see
which watched ids it mentions (`<@U…>`, `<!subteam^S…>`), and builds a `Message` that has **no
text field**. It holds channel, ts, thread ts, user, username, bot flag, channel type and the set
of mentions. No event, series, log line or error carries text. `tests/fake_slack.py` puts a canary
in every message, and the tests check that the canary never reaches the bus, the metrics, the log
or any file. The opt-in contract test (`ORCHESTRA_BRIDGES_SLACK_CONTRACT=1`, with `SLACK_CANARY`)
checks the same against the real API.

### 7.11 Identity on Slack

A Slack account in the map is a member id or a handle (`slack = "U012AB3CD"`). An account not
listed is **namespaced**: `person.slack.<handle>` or `bot.slack.<handle>`. A stranger who shares a
login with someone on GitHub is therefore a different machine. Only the map joins accounts across
systems. GitHub's unlisted sources keep the bare `person.<login>`, the form in which they were
first published.

---

## 8. Contracts

### 8.1 Events

Every event is a CloudEvents 1.0 envelope written by `orchestra.events.Events.publish`, behind
`relay.Relay`, which refuses any source but `person.…` or `bot.…`. Bridged events carry the
system's own `id` and `time`. No bridge publishes as itself.

| Type | Status | Source | Subject | Id | Data (never text) |
|---|---|---|---|---|---|
| `github.issue.opened` | main | the author (`person.*`/`bot.*`) | `<owner>/<repo>#<n>` | `github:<repo>:opened:<n>` | `repository`, `issue`, `login`, `association` |
| `github.issue.commented` | main | the commenter | same | `github:<repo>:comment:<id>` | `repository`, `issue`, `login`, `comment` (id), `association` |
| `github.issue.labelled` | main | who added the label | same | `github:<repo>:event:<id>` | `repository`, `issue`, `login`, `label` (name) |
| `github.issue.closed` | main | who closed it | same | `github:<repo>:event:<id>` | `repository`, `issue`, `login`, `reason` (`state_reason`) |
| `github.issue.reopened` | main | who reopened it | same | `github:<repo>:event:<id>` | `repository`, `issue`, `login` |
| `orchestra.machine.started` (actor) | main | the actor | none | `<system>:announce:<source>:<hash12>:<time>` | `kind` (`person`/`program`), `mode` (`ad hoc`), `package` (`bridges`), `<system>` (login), `bot`, `name`?, `team`? |
| `orchestra.machine.started` (the system itself) | main (Jira) | `bot.jira` | none | `jira:announce:bot.jira:<hash12>:<time>` | `kind` (`program`), `mode` (`ad hoc`), `package`, `bot` (true), `name` (`Jira`); no account |
| `slack.ping.received` | main | the pinger | `<workspace>/<channel>/<thread ts>` | `slack:<ws>:ping:<ch>:<ts>` | `workspace`, `channel`, `channel_type` (`im`/`mpim`/`group`/`channel`), `ts`, `thread_ts`, `user` (member id), `kind` (`dm`/`mention`/`group`) |
| `slack.ping.answered` | main | the owner | same | `slack:<ws>:answered:<ch>:<ping ts>` | `workspace`, `channel`, `ts`, `wait_seconds`, `within`, `rule` (`reply`/`reaction`). Reply adds `reply_ts`. Reaction adds `reaction`, `observed: true`, `not_before` |
| `github.pr.*` (13 types) | main | who acted | `<owner>/<repo>#<n>` | `github:<repo>:pr:<n>:<kind>:<GitHub id>` | §12.3 |
| `jira.issue.created`, `.assigned`, `.transitioned`, `.updated`, `jira.comment.added` | main | who acted (`person.<key>`, `person.jira.<account>`), or `bot.jira` for Jira's own change (no author) | the issue key, `ABC-12` | `jira:<site>:issue:<issue id>:<created, history:<id>:<kind>, comment:<id>>` | §13.2 |

Events published before 2026-10-27 have unprefixed types (`issue.opened`, …). They are still read
back on a restart. A bus from before 2026-10-27 also holds events no bridge publishes any more:
`bridge.gap` (now counted, §5.5), `slack.ping.expired` (now counted, §7.5), the bridges' own
`orchestra.machine.started`, and Jira's own changes as `bridges.jira`. Slack's expired events are
still read back by their ids; nothing reads the rest.

### 8.2 Series

| Series | Status | Written how | Labels |
|---|---|---|---|
| `github_issues_unanswered`, `github_issues_unanswered_oldest_seconds`, `github_issues_opened_total`, `github_issues_answered_total{within}`, `github_issues_closed_unanswered_total`, `github_issue_first_response_seconds_total` | main | Served at `/metrics`, each point at the time of its change, and stored by the instrument `bridges.github` the plugin declares | `repo`, plus `instrument="bridges.github"` and `project` from the scrape (`machine="bridges.github"`, written by the bridge itself, before 2026-10-27) |
| `<prefix>_bridge_gaps_total`, `<prefix>_bridge_gaps_recovered_total`, `<prefix>_bridge_gap_seconds_total`, prefix `github_issues`, `github_prs`, `jira`, `slack` | main | Served at `/metrics` beside each bridge's other series; continued from the store on start (`gaps.py`) | the bridge's scope (`repo`, `workspace`; none for Jira, whose instrument is per site), plus `instrument` and `project` |
| `slack_pings_unanswered`, `slack_pings_oldest_unanswered_seconds`, `slack_pings_received_total`, `slack_pings_answered_total{within}`, `slack_pings_expired_total`, `slack_ping_wait_seconds_total`, `slack_bridge_last_poll_timestamp_seconds` | main | Served at `/metrics` and stored by the instrument `bridges.slack` the plugin declares | `workspace`, plus `instrument="bridges.slack"` and `project` from the scrape (`machine="bridges.slack"` before 2026-10-27) |
| `github_prs_open`, `github_prs_awaiting_review`, `github_prs_awaiting_changes`, `github_prs_approved`, `github_prs_awaiting_label_result`, `github_prs_checks_failing`, `github_prs_review_request_oldest_seconds`, `github_prs_bridge_last_poll_timestamp_seconds` | main | Served at `/metrics` (levels read from GitHub each round) and stored by the instrument `bridges.github_prs` the plugin declares | `repo`, plus `instrument="bridges.github_prs"` and `project` from the scrape |
| `jira_issues{jira_project, category}`, `jira_issues_mine_not_started`, `jira_comments_unanswered`, `jira_comment_unanswered_oldest_seconds` (each `{jira_project}`), `jira_bridge_last_poll_timestamp_seconds` | main | Served at `/metrics` (read from Jira each round; `done` every `done_every`) and stored by the instrument `bridges.jira` the plugin declares | `jira_project`, `category`, plus `instrument` and `project` from the scrape |
| `rabbit:rabbitmq_published_total`, `rabbit:rabbitmq_acked_total`, `rabbit:rabbitmq_backlog`, `rabbit:rabbitmq_consumers` (prefix = the source's) | main | Read by Orchestra's sources from the store that scrapes RabbitMQ. Nothing is written | `vhost`, `exchange`, `queue`, `consumer` → `machine = rabbit.<consumer>`, with traffic metadata in provenance |

### 8.3 Configuration keys

All keys live in the project's `orchestra.toml`. Orchestra ignores `[bridges.*]`. A bridge runs
only when its section exists.

| Key | Default | Notes |
|---|---|---|
| `[bridges] people` | none | Path to the identity map, relative to the repository. Shared by every bridge |
| `[bridges.github] repos` | none: required | `owner/name` strings; a single string is accepted |
| `every` | `60s` | Pause between rounds; at least 5 s |
| `lookback` | `1h` | First start only: how far back to read |
| `max_catchup` | `48h` | After a gap, how far back to read at most |
| `overlap` | `10m` | Re-read before the last poll. Also the delay before an event is counted |
| `bots_answer` | `false` | Whether a bot's comment or close is a first response |
| `host`, `port` | `127.0.0.1`, `9464` | Where `/metrics` listens, and where the plugin's instrument scrapes it. `port = 0`: any free port, and no instrument |
| `api` | `https://api.github.com` | GitHub Enterprise, or a fake |
| `[bridges.slack] every`, `lookback`, `max_catchup`, `overlap` | `60s`, `1h`, `48h`, `10m` | As for GitHub; `every` at least 5 s |
| `answered` | `["reaction"]` | `reaction`, `reply`, or both; never empty |
| `reactions` | `["white_check_mark", "heavy_check_mark"]` | Colons are stripped |
| `groups` | `[]` | User group ids, `S…` |
| `dms` | `true` | Whether a DM is a ping |
| `expire` | `168h` | How long a ping may wait before it stops counting |
| `host`, `port` | `127.0.0.1`, `9465` | Where `/metrics` listens, and where the plugin's instrument scrapes it. `port = 0`: any free port, and no instrument |
| `token_file` | `~/.config/orchestra-bridges/slack-token` | Never the token itself. A `token` key is refused |
| `queries` | the defaults (§7.2) | Replaces the ping searches; `{me}` is the owner's member id |
| `api` | `https://slack.com/api` | Only `https` on slack.com, or loopback |
| `[bridges.github_prs] repos` | none: required | `owner/name` strings |
| `branches`, `labels` | `[]`, `[]` | Follow pull requests whose branch matches a glob, or that carry a label; with neither, all |
| `every`, `lookback`, `max_catchup`, `overlap` | `60s`, `1h`, `48h`, `10m` | As for issues |
| `checks` | `true` | Read the head's check runs and statuses each round |
| `host`, `port` | `127.0.0.1`, `9466` | Where `/metrics` listens and the plugin's instrument scrapes it. `port = 0`: any free port, no instrument |
| `[bridges.github_prs.label_results]` | `{}` | A label (`SAIL`) and the labels that report its result; only the level `github_prs_awaiting_label_result` reads it |
| `api` | `https://api.github.com` | As for issues. A `token` key is refused |
| `[bridges.jira] api` | none: required | The Data Center base URL or a Cloud site; https, or loopback for a fake |
| `deployment` | `datacenter` (`cloud` on `*.atlassian.net`) | `datacenter` or `cloud` |
| `projects`, `jql` | one of the two, required | Project keys, or a JQL clause without `ORDER BY` |
| `every`, `lookback`, `max_catchup`, `overlap` | `60s`, `1h`, `48h`, `10m` | As for GitHub |
| `done_every` | `1h` | How often done issues are counted; 0: never |
| `bots_ask` | `false` | Whether a bot's comment waits for the owner |
| `sprint_field` | found from `/field` | `customfield_<n>` |
| `token_file`, `email` | `~/.config/orchestra-bridges/jira-token`, none | Never the token itself (a `token` key is refused). `email` is Cloud's only |
| `host`, `port` | `127.0.0.1`, `9467` | `/metrics`, and the plugin's instrument. `port = 0`: no instrument |
| `[bridges.jira_<name>]` | (none) | A second site: the same keys, `port` required |
| `[[scrape]] name = "bridges.<section>"` | (none) | Optional: replaces the plugin's instrument of that name, to scrape more often or elsewhere (§4.3). It must keep the name |
| `[[sources]] preset = ".../rabbitmq/preset.toml"`, `[sources.producers]` | (none) | Counted traffic (§6) |

### 8.4 Credentials

| Bridge | Credential | Where from |
|---|---|---|
| GitHub issues and pull requests | A token that can read the repositories. One is required, even for public repositories: the bridge exits 2 without one | `GH_TOKEN`/`GITHUB_TOKEN` (github.com) or `GH_ENTERPRISE_TOKEN`/`GITHUB_ENTERPRISE_TOKEN` (other hosts), else `gh auth token --hostname <host>` |
| Slack | User token `xoxp-…` with `search:read` and `reactions:read` | `SLACK_USER_TOKEN`, else a `0600` file outside the repository (`SLACK_USER_TOKEN_FILE`, `token_file`, `~/.config/orchestra-bridges/slack-token`; `secret.py`, §7.9) |
| Jira | Data Center: a personal access token, as `Bearer`. Cloud: an API token with the account's email, as Basic | `JIRA_API_TOKEN` (`JIRA_<NAME>_API_TOKEN` for a second site), else a `0600` file outside the repository (`JIRA_API_TOKEN_FILE`, `token_file`, `~/.config/orchestra-bridges/jira-token`). Cloud's email from `JIRA_EMAIL` or `email` |
| RabbitMQ preset | Whatever the store needs | Orchestra's `[[sources]] auth`, from an environment variable or a file outside the repository. The rig needs none |

Every bridge reads its token once at start, keeps it in memory, and sends it only to the
configured API origin.

### 8.5 What it never does

- **Never text.** No titles, bodies, comments, messages or document content in events, series,
  logs, errors or files. Canary tests guard this for each bridge (`tests/unit/test_bridge.py`,
  `test_github_prs_bridge.py`, `test_jira_bridge.py`, `test_slack_bridge.py`).
- **Never writes back.** No comments, labels or reactions, and nothing marked read. The only HTTP
  method used is GET. Speaking into another system is a separate proposal, the outbox.
- **Never turns counts into events.** The preset publishes nothing.
- **Never keeps state of its own.** It writes no file and no database, only the bus (as the actors).
- **Never acts as a machine.** It never announces itself, never publishes under its own name, is
  never declared as a machine and never writes a metric (`relay.py`, `plugin.py`; checked by
  `tests/relayed.py` after every bridge's run against its fake).
- **Never sends the token elsewhere**, never logs it, never prints it in a `repr`.
- **Never imports Orchestra's web app, model or host.** It uses only `orchestra.events`,
  `orchestra.metrics`, `orchestra.config`, `orchestra.plugins`, `orchestra.launcher` and
  `orchestra.collect.spec`. (The tests use Orchestra's Machines view and scrape parsing to check it.)

---

## 9. Add a new bridge

Every bridge follows the same pattern: a relay that publishes as the actors and serves `/metrics`
for an instrument. The GitHub issues bridge shows serving points with their own times; the
pull-request, Jira and Slack bridges serve current values.
Proposed next bridges: Confluence, Google Drive, and the Bluesky stand-in.

### 9.1 The pattern

```mermaid
flowchart TB
    start["run(stop)"] --> res["resume()<br/>read own events back (ids #lt;system#gt;:…)<br/>seen, announcements, replayed counts<br/>store: watermark / last poll, counters"]
    res --> ann["reannounce() map changes<br/>(never the bridge itself)"]
    ann --> round["round(): poll each scope"]
    round --> win["_window(now)<br/>start = max(since, now − max_catchup)<br/>gap if last good poll > 3 rounds ago"]
    win --> read["read the system (GET only)<br/>paged, Retry-After honoured"]
    read -- error --> logf["log, publish nothing"] --> wait
    read -- ok --> gap["count the gap (once) if any:<br/>gaps.close(), never an event"]
    gap --> pub["for each new item not in seen:<br/>announce actor if changed;<br/>publish as the actor (relay.py), with the system's id and time"]
    pub --> cnt["fold into counts<br/>serve /metrics (the instrument stores them)"]
    cnt --> adv["last_ok = now; since = now − overlap"]
    adv --> wait["stop.wait(every)"] --> round
```

### 9.2 Checklist

1. **`config.py`**: add a `<System>Config` dataclass and a parser for `[bridges.<system>]`. Reuse
   `section()`, `people_file()` and `orchestra.config.parse_duration`. Keep the common keys:
   `every` (at least 5 s), `lookback`, `max_catchup`, `overlap`. Refuse any key that would hold a
   secret. Restrict `api` to the real host or loopback.
2. **`<system>/api.py`**: a read-only client:
   - token in a private attribute, with a `repr` that hides it;
   - a `_SameOrigin` redirect handler, and a check before every request that the URL is the
     configured origin;
   - `Retry-After` honoured with a cap;
   - errors that name the method or path and a code, never a body.
3. **Reading** (`poll.py` or `search.py`): everything since `t`, paged. Turn each item into a
   metadata-only record **at the point of reading**, so text never leaves that function.
4. **Translation** (`translate.py` or `pings.py`):
   - types `<system>.<noun>.<verb>`;
   - ids `<system>:<scope>:<what>:<system's own id>`, stable across reads;
   - the system's time;
   - a subject naming the item, so hand-offs work.
5. **Identity**: `People.actor(system, login, bot)`. A new system passes
   `namespaced=True` (as Jira and Slack do), so strangers are `person.<system>.<login>`. Add `<system> = "<account>"` to
   the identity map's entries. What the system does with no account behind it is
   `People.itself(system, "<Name>")`: `bot.<system>`, never the bridge.
6. **The relay and announcements**: wrap the events in `relay.Relay(events)` (it refuses any
   source but `person.…` or `bot.…`) and use `announce.Announcer(relay, system, people)`. Call
   `actor()` before each actor's event and `reannounce()` at start. Never announce the bridge.
7. **Counts**: a fold over events in time order, rebuilt from the bus at start, with counters that
   continue from the store (`continue_from`) and never go down. Serve them on `/metrics`
   (`serve.MetricsServer`), with a timestamp per point when the true times matter (GitHub
   issues), or as current values (pull requests, Jira, Slack); add the default port to `PORTS`
   in `plugin.py`, which then declares the scrape (§4.3). Read the counts back by
   `instrument="bridges.<section>"`, and serve a `*_last_poll_timestamp_seconds` value (or, as
   GitHub issues does, re-serve every series each poll) for resume. Never write a metric.
8. **`bridge.py`**: `resume`, `reannounce`, `_window` (`gaps.Gaps.late`), `poll` (count a closed
   gap with `Gaps.close`; serve `Gaps.values` with the other series), `round` (catch the API
   error), and `run(stop, once=)`. `bridges.<section>` is only the name in its log lines.
9. **`__main__.py`**: add a builder `(project, stop)` to `BRIDGES`. A token that is not a CLI
   login comes from `secret.read_token`.
10. **`plugin.py`**: add the system to `SYSTEMS` and its default port to `PORTS`. That declares
    the process `bridges.<system>` and its scrape (`collectors`). Never a machine.
11. **Tests**, per the AGENTS.md conventions:
    - `tests/fake_<system>.py` in the real shapes, with a canary in every text field;
    - unit tests for publishing, re-polls, restarts, gaps, the canary, and the token never
      leaving; after a run against the fake, `relayed.only_actors(bus)` (no `bridges.*` source,
      no announcement from the bridge, only people and bots in Orchestra's Machines view);
    - an e2e test through `orchestra run`;
    - an opt-in contract test comparing the fake's shapes with the real API's (keys and types,
      never content).
12. **Docs**: README (events, series, configuration, limits), SPEC (a module table), and this
    document.

### 9.3 Shared modules

| Module | Used by | Provides |
|---|---|---|
| `config.py` | every bridge | `section()`, `people_file()`, `_names()`, per-bridge dataclasses and parsers |
| `identity.py` | every bridge | `People.load/actor/accounts/listed`, `namespaced=`, `Actor.announcement(system)` |
| `announce.py` | every bridge | `Announcer.from_events/actor/reannounce`, `prefix(system)` |
| `relay.py` | every bridge | `Relay`: publishes only as `person.…` or `bot.…`; `after` passed through |
| `gaps.py` | every bridge | `Gaps`: `late`, `close`, `values`, `resume`: gaps counted, never published |
| `serve.py` | every bridge | `MetricsServer`: `GET /metrics`, 503 until ready |
| `plugin.py` | every bridge | `SYSTEMS`, `PORTS` and `metrics_url`: process and instrument declarations (no machines) |
| `secret.py` | Jira, Slack | `read_token(name, default_file, repo_root, token_file)`: the environment, else a private file outside the repository |
| `__main__.py` | every bridge | The `BRIDGES` registry (and `jira[_<name>]`): the process entry point |

---

## 10. Proposed, not built

From Orchestra's `docs/proposals/event-sources.md`, after the owner's answers of 2026-10-26.

- **Bridges:**
  - Confluence;
  - Google Drive (`gdoc.*`);
  - Bluesky's Jetstream, chosen with the owner as the second test bridge and a high-volume
    stand-in for Slack.
- **Push:** webhooks and Socket Mode, where a relay is allowed. Today everything polls, because
  the laptop has no public URL.
- **Counted traffic:**
  - the GCP Pub/Sub preset, its topology poller (`[[collect]]` over the admin API) and its
    simulator;
  - Kafka and NATS presets;
  - IAM-candidate publishers and a fitted split;
  - loose balances.
- **People:** a team-only mode. The proposal's pseudonym default is **superseded** by the owner's
  answer 6 (people by name, each with a team).
- **Slack:** working hours for the 15 minutes (an open question); the work workspace (allowed by
  the owner's answer 5, but not yet run there).
- **The outbox:** speaking back into Slack or Jira. Bridges stay read-only until then.
- **Layout drift from the proposal's §7:** the proposal sketched `envelope.py`, `fakes/` and
  `pubsub/`. The built code uses per-bridge `translate.py`/`pings.py`, `tests/fake_*.py` and
  `rabbitmq/`.

## 11. Where the docs and the code disagree

Found while checking this document against the code. The code is taken as the truth everywhere
above.

1. **README event table (main).** The README says `github.issue.labelled`, `.closed` and
   `.reopened` carry "the same" data as `opened`, including `association`. `translate.from_event`
   does not include `association` for these three; issue events carry no author association.
2. **README "Every `every` seconds it reads" (main).** `every` is the pause after a round
   (`stop.wait(every)`), not the period. Slack's README says the same and has the same gap.
3. **Slack `bridge.py` docstring.** It says "Only these [the searches] (and `auth.test`)
   fail a poll". `auth.test` is not called in a poll. It is called once in `run()` before
   `resume()`, retried each round until it succeeds.
4. **Proposal vs the built Slack bridge.**
   - The proposal's §5 names two events and the buckets `5m|15m|60m|over`, with the reply as the
     default answer. The bridge adds the bucket `unknown` and the checkmark default, following the
     owner's answer 5, and counts expiries (`slack_pings_expired_total`) without an event.
   - The proposal's §4 sources (`work.person.7f3a`, `work.jira`) became `person.<key>` and, for a
     system's own act, `bot.<system>` (`bot.jira`); never the bridge (D19).
5. **Proposal §4 privacy** (pseudonyms by default) is superseded by the owner's answer 6. The
   proposal text was not updated.
6. **Orchestra's AGENTS.md rule 2** lists the announcement fields Orchestra reads as "mode, what it
   listens for, its kind, where its log is and which package it belongs to". Orchestra's SPEC and
   `web/machines.py` also read `name` and `group`/`team`, which this package relies on for people's
   names and teams.
7. **The gap and resume behaviour differs between the GitHub issues and Slack bridges** (from the
   start vs the end of the last poll; stored counters vs the larger of stored and rebuilt). This is
   intended and documented in SPEC §4d. It is noted here because readers of the
   GitHub section may assume the two bridges behave the same.

## 12. The GitHub pull-request bridge

Goal G6 in Orchestra's `docs/roadmap/goals.md`: pull requests tracked to resolution. The owner's
company drives CI/CD with labels on pull requests: the owner adds `SAIL` by hand, and the pipeline
later reports pass or fail, also as labels. Something must watch each pull request and, when review
comments, change requests or a failing label arrive, re-engage the worker that wrote the change.
Re-engaging is the task system's job (its protocol lives in `~/orchestra-tasking` and
`~/orchestra-agents`, not here). This bridge only reads GitHub and publishes what happens, with what
that wiring needs: the pull request, its branch, its head commit, who acted and what kind of thing
happened.

### 12.1 Why a bridge of its own

The issues bridge drops pull requests on purpose, and extending it would have tangled two rhythms:

| | Issues bridge | Pull-request bridge |
|---|---|---|
| Reads | three repository-wide lists since `t` | the open pulls, the recently closed pulls, then per followed pull request its timeline and its head's checks |
| Which items | every issue | those whose branch or labels match, followed to resolution |
| Why per item | not needed | a review does not always move a pull request's `updated_at` (seen on the real API), so `since` cannot say which to re-read |
| Measures | counts rebuilt from events, served with their own times for the instrument `bridges.github` | levels read from GitHub, served on `/metrics` for the instrument `bridges.github_prs` |

Both share `github/api.py` (the client and `gh_token`; `get` was added for single objects), the
identity map, and the announcer, so a person is one machine (`person.<login>` or their map key)
whichever bridge saw them. Announcements of both use ids `github:announce:…`.

### 12.2 Modules

| Module | Responsibility |
|---|---|
| `github_prs/poll.py` | The reads (GET only), each paged where GitHub pages |
| `github_prs/translate.py` | `Pull` (a pull request as listed now) and `Seen` records: type, stable id, GitHub's time, metadata-only data |
| `github_prs/levels.py` | `PullState` per open followed pull request (verdicts, review requests, checks), the levels, and their Prometheus text |
| `github_prs/bridge.py` | Resume, follow, read then publish, pushes, announce the actors, count gaps, `/metrics`, the loop |
| `serve.py` | `MetricsServer`, shared with the Jira and Slack bridges |
| `announce.py` | `Announcer`, shared with every bridge |
| `config.py` | `GitHubPRsConfig`, `github_prs()`, `follows(branch, labels)` |

### 12.3 Events

Every event: source the actor, subject `<owner>/<repo>#<n>`, GitHub's time, id
`github:<repo>:pr:<n>:…`. Every `data` holds `repository`, `pull`, `login`, `branch`, `base` and
`head` (the commit the event is about: the commit reviewed, the new head, the commit checked; else
the head when read). Never a title, body, review text, comment, diff hunk or commit message.

| Type | Source | Id suffix | Extra data |
|---|---|---|---|
| `github.pr.opened` | the author | `opened` | `association`, `draft`. `head` is the head at opening, from the timeline |
| `github.pr.pushed` | force pusher, else the head commit's GitHub committer or author, else the pull request's author | `push:<sha>` | `previous`, `forced`, `pusher_from` |
| `github.pr.review_requested`, `github.pr.review_request_removed` | who asked or withdrew | `event:<issue event id>` | `reviewer` or `team` |
| `github.pr.review_submitted` | the reviewer | `review:<review id>` | `review`, `state` (`approved`, `changes_requested`, `commented`, `dismissed`), `comments` (count), `association` |
| `github.pr.review_commented` | the reviewer | `review_comment:<comment id>` | `review`, `comment`, `path`, `line`, `start_line`, `side`, `subject_type`, `in_reply_to` |
| `github.pr.commented` | the commenter | `comment:<comment id>` | `comment`, `association` |
| `github.pr.labelled`, `github.pr.unlabelled` | who changed it | `event:<issue event id>` | `label` |
| `github.pr.check_completed` | `bot.<app slug>` for a check run, the creator for a status | `check:<run id>`, `status:<status id>` | `check`, `conclusion`, `via`, `app` |
| `github.pr.merged` | who merged | `event:<issue event id>` | `merge_commit` |
| `github.pr.closed` | who closed it, unmerged (a merge's own close, within 5 s of it, is left out) | `event:<issue event id>` | `merged: false` |
| `github.pr.reopened` | who reopened | `event:<issue event id>` | none |

Not published: drafts made ready or turned back, assignments, reviews still pending, a review's
dismissal as an event of its own (the review's state becomes `dismissed`; a review first seen after
it was dismissed is published with that state), and check runs still running or statuses pending.

### 12.4 One poll

```mermaid
flowchart TB
    w["_window(now): start = max(since, now - max_catchup), gap if last good poll over 3 rounds ago"]
    w --> lists["GET pulls?state=open (all pages)<br/>GET pulls?state=closed, newest updated first, until before start"]
    lists --> follow["follow: already followed, or branch glob or label matches<br/>(closed ones only if opened after start)"]
    follow --> each["per pull request: GET its timeline,<br/>GET check-runs and statuses of its head (checks = true)"]
    each --> new["new review: GET its line comments (once per pull request)<br/>new head: GET the commit, for who pushed"]
    new -- "any read fails" --> fail["log it, publish nothing, try again next round"]
    new -- "all read" --> gap["count the gap once, if any (no event)"]
    gap --> pub["each record not seen, in time order:<br/>announce the actor if new or changed, publish"]
    pub --> lv["levels of the open followed pull requests, served on /metrics"]
    lv --> adv["last_ok = now, since = now - overlap"]
```

A round costs about 2 requests per repository and 3 per followed pull request, plus one per new
review and one per new head: ten open pull requests polled every minute use about 1,900 of the
5,000 an hour.

### 12.5 Resume and gaps

As for issues (§5.4, §5.5), with what is resumed adapted:

- `seen`: every id on the bus starting `github:<repo>:pr:`;
- followed pull requests: those with a `github.pr.opened` on the bus;
- heads: each pull request's `head` in its `opened` and `pushed` events, in time order. A head not
  among them is a push;
- `last_ok`: the value of `github_prs_bridge_last_poll_timestamp_seconds{instrument="bridges.github_prs", repo}`
  in the store. It is the poll's start, served by the bridge and stored by the scrape, so a scrape
  that kept running while polls failed does not hide a gap;
- `since`: the later of the last event and `last_ok`, less `overlap`; else `now - lookback`.

The levels need no resume: each round reads them from GitHub. The gap counters continue from the
store, by the same `instrument` and `repo`.

### 12.6 The owner's loop, as the bridge sees it

The task system's part is drawn dashed: it is what the events are for, not built here.

```mermaid
sequenceDiagram
    autonumber
    participant W as Worker (agent)
    participant O as Owner
    participant G as GitHub
    participant CI as Pipeline (label-driven)
    participant B as bridges.github_prs
    participant Bus as Orchestra bus
    participant T as Task system

    W->>G: push branch agent/fix-login, open pull request 12
    B->>G: GET pulls, timeline, checks (every 60 s)
    B->>Bus: github.pr.opened (source the author, subject acme/service PR 12, branch, head)
    O->>G: add label SAIL
    B->>Bus: github.pr.labelled SAIL (source person.owner)
    CI->>G: add label SAIL-failed
    B->>Bus: github.pr.labelled SAIL-failed (source the pipeline's account)
    Note over B: level github_prs_awaiting_label_result drops by one
    G->>G: reviewer requests changes with two line comments
    B->>Bus: github.pr.review_submitted state changes_requested, comments 2
    B->>Bus: github.pr.review_commented, twice (path, line, side)
    Bus-->>T: events for the pull request and branch of a ticket
    T-->>W: re-engage the worker that wrote it (its session, same branch)
    W->>G: push a new commit to agent/fix-login
    B->>Bus: github.pr.pushed (previous head, new head)
    G->>G: reviewer approves, owner merges
    B->>Bus: github.pr.review_submitted approved, then github.pr.merged
    Bus-->>T: merged, so the ticket can close
```

### 12.7 What the task system needs to wire these to re-engage a worker

The bridge publishes; it decides nothing. The task system (being redesigned elsewhere) will need:

1. **The link from a ticket to a pull request.** Match on `data.repository` and `data.pull` (or
   the subject), or on `data.branch` when the ticket knows its branch before the pull request
   exists. Every event carries both.
2. **Which events re-engage.** Suggested: `review_submitted` with `state` `changes_requested` (or
   `commented` with `comments` above 0), `review_commented`, `commented`, `check_completed` with a
   failing `conclusion` (`failure`, `timed_out`, `error`), and `labelled` with a label the task
   system's own configuration calls failing (`SAIL-failed`). Which labels mean failure is the
   owner's configuration, not the bridge's: the bridge only names labels.
3. **Batching.** One review arrives as one `review_submitted` followed by its `review_commented`s
   at the same time; wait for the review, not each comment, and use `comments` to know how many to
   expect. Several events often land in one poll.
4. **The commit the feedback is about.** `data.head` on a review is the commit reviewed; compare it
   with the branch's head before re-engaging, so stale feedback is recognised.
5. **Its own echo.** A worker that pushes or comments through the owner's GitHub account appears as
   the owner (`person.<owner>`), and `pusher_from` may be `pull_author`. To ignore its own acts,
   the task system should match on what it did (the head it pushed, the time) rather than on the
   source alone.
6. **Resolution.** `merged` resolves the pull request; `closed` (never a merge's own close) means
   it was abandoned; `reopened` brings it back.
7. **Exactly once.** Ids are stable, and a restart or a gap never republishes, but a consumer that
   itself restarts should still key on the event id.
8. **Waiting, as levels.** `github_prs_awaiting_changes` and `github_prs_awaiting_label_result` are
   the stocks of work waiting on workers and on the pipeline; `github_prs_awaiting_review` and its
   oldest age, on reviewers.

### 12.8 The sandbox and what the end-to-end run showed

`danroblewis/orchestra-e2e-sandbox` is a private repository made for this test, holding a README,
a small file and one file per run under `runs/`. `tests/contract/test_github_prs_sandbox.py`
(opt in, `ORCHESTRA_BRIDGES_SANDBOX=1`) acts there with `gh` and `git` as the owner: it opens a pull
request from a new branch `e2e/<time>-<random>`, submits a review with one line comment, adds and
removes the label `e2e-sail` (standing in for SAIL), sets a failing commit status `e2e/sail`
(standing in for the pipeline's result), pushes a second commit and merges, polling the bridge
against the real API between steps.

The first run, on 2026-10-27, published exactly `opened`, `review_submitted` (`commented`, 1
comment), `review_commented`, `labelled`, `unlabelled`, `check_completed` (`failure`), `pushed`
(previous and new head right) and `merged`, each once, all as `person.danroblewis` on the pull
request, and no `closed` for the merge. A restart with the instrument's last good poll set before
the run recorded one `bridge.gap` (`recovered: true`; a gap is counted now, not published) and
published nothing again. **GitHub refused
"request changes"** on one's own pull request (HTTP 422, "Can not request changes on your own pull
request"), so the test falls back to a commenting review, and `changes_requested` is covered by the
fake only, as is a review request (the sandbox has no second account to ask).

### 12.9 Limits

- Several pushes between two polls are one `pushed`; for an ordinary push GitHub names no pusher.
- A push's time is its commit's date when that falls in the window read, else when it was seen.
- Only the first 100 check runs of a head are read.
- `review_request_removed` and `review_requested` shapes are from GitHub's documentation and the
  fake; the sandbox cannot produce them with one account.
- Followed pull requests are read every round whether or not anything changed, which is what costs
  requests (§12.4).

## 13. The Jira bridge

Building block 4 of Orchestra's `docs/roadmap/story-placement.md`. It unlocks the Jira half of ten
of the owner's 21 user stories (1, 2, 3, 6, 8, 10, 12, 14, 15, 20): a request filed or assigned to
the owner, a CCB ticket approved, a customer ticket's SLA, tickets in progress with no activity, the
weekly update's done work. The owner's Jira is **Data Center** (REST API v2, a personal access
token). Cloud (API v3, an API token with the account's email) is kept because it differs only in
the search's paging and the sign-in.

### 13.1 Modules

| Module | What it does |
|---|---|
| `jira/api.py` | Read-only client. Bearer (Data Center) or Basic (Cloud); `/search` paged by `startAt` and `total`, or `/search/jql` by `nextPageToken` and `isLast`; an issue's whole changelog (`/issue/<key>?expand=changelog`, or Cloud's `/issue/<key>/changelog` pages) and comments (`/issue/<key>/comment`); `Retry-After` on 429 and 503, capped at 900 s, 3 retries; same-origin redirects only; errors name the path and code |
| `jira/records.py` | JSON to `Issue`, `History`, `Change`, `Comment`, `User` records holding no text. Only the changes the bridge publishes are kept: status, assignee, sprint, priority, labels, link, due date. Data Center user keys in a change of assignee are turned back into usernames |
| `jira/poll.py` | `site` (`/myself`, `/field` for the sprint field, `/status` for every status's category), `updated`, `open_issues`, `done_count`, `mine` |
| `jira/translate.py` | Records to `Seen` events with stable ids |
| `jira/levels.py` | The levels and their Prometheus text |
| `jira/bridge.py` | Resume, the window and the gap (counted), read then publish as the actors (`bot.jira` for none), announce them, `/metrics`, the loop |
| `secret.py` | The token from `JIRA_API_TOKEN`, the file `JIRA_API_TOKEN_FILE` names, `token_file`, or `~/.config/orchestra-bridges/jira-token`; a file must be outside the repository and `chmod 600` |

### 13.2 Events

The source is who acted, through the identity map's `jira` key (§13.5); the subject the issue key;
the time and id Jira's.

| Type | From | Id (after `jira:<site>:issue:<issue id>:`) | Data besides the common fields |
|---|---|---|---|
| `jira.issue.created` | the issue's `created` and `creator` (else `reporter`) | `created` | `status`, `status_id`, `category`: where it started (the first status change's `from`, else its status now) |
| `jira.issue.assigned` | an assignee at creation (the first assignee change's `from`, else the assignee now) | `created:assignee` | `assignee`, `previous` (`null`), `assignee_machine` |
| `jira.issue.assigned` | a changelog item `assignee` | `history:<id>:assignee` | `assignee`, `previous`, `assignee_machine` |
| `jira.issue.transitioned` | a changelog item `status` whose `from` and `to` ids differ | `history:<id>:status` | `from`, `to` (names), `from_id`, `to_id`, `from_category`, `to_category` (from `/status`) |
| `jira.issue.updated` | changelog items `Sprint` (or the sprint field's id), `priority`, `labels`, `Link`, `duedate` in one history | `history:<id>:updated` | `fields`, `changes`: `{field, from, to}` (priority ids, dates, issue keys, sprint id lists) or `{field: labels, added, removed}` |
| `jira.comment.added` | a comment | `comment:<id>` | `comment`, `comments` (its ordinal), `author`, `restricted` |

Common data: `site`, `project`, `issue`, `key`, `issuetype`, `login`, and the issue as read:
`status_category`, `priority`, `priority_id`, `labels`, `sprints`, `links`. Never a summary, a
description, a comment body, a sprint's name or goal, or a text field's before and after.

### 13.3 One poll

Every request is a GET. A poll reads everything first and publishes only when every read succeeded.

```mermaid
sequenceDiagram
    autonumber
    participant B as bridges.jira
    participant J as Jira (Data Center, /rest/api/2)
    participant Bus as Orchestra bus
    participant I as instrument bridges.jira
    participant S as Metrics store

    Note over B: first poll only (and when a status id is new)
    B->>J: GET /myself, /field, /status
    J-->>B: the owner, the sprint field, each status's category
    B->>J: GET /search (scope) AND updated >= "-Nm", expand=changelog, paged by startAt
    J-->>B: issues with fields, changelog, comments
    opt a changelog or comment list cut short
        B->>J: GET /issue/KEY?expand=changelog, /issue/KEY/comment
    end
    B->>J: GET /search (scope) AND statusCategory != Done
    B->>J: GET /search (scope) AND statusCategory != Done AND assignee = currentUser(), with comments
    opt done_every has passed
        B->>J: GET /search (scope) AND statusCategory = Done, keys only
    end
    alt any read fails (asleep, 5xx, 429 past its retries)
        J--xB: error
        Note over B: log the path and code, publish nothing, try again next round
    else every read succeeds
        opt more than 3 rounds since the last good poll
            Note over B: count the gap (jira_bridge_gaps_total, …), no event
        end
        loop each event not yet published, in Jira's time order
            B->>Bus: orchestra.machine.started for the actor (if new or changed)
            B->>Bus: jira.* with Jira's id and time, subject the key, as the actor (bot.jira if none)
        end
        Note over B: levels measured, last_ok = now, since = now - overlap
    end
    I->>B: GET /metrics (every 30 s)
    B-->>I: jira_issues, ..._mine_not_started, ..._comments_unanswered, ..._oldest_seconds, gap counts, last poll
    I->>S: points labelled instrument=bridges.jira, project
```

### 13.4 Resume and gaps

As the pull-request bridge (§12.5): own events read back by the id prefix `jira:<site>:issue:` give
`seen`; the store's `jira_bridge_last_poll_timestamp_seconds{instrument="bridges.jira"}` gives the
last good poll; the next poll reads from the later of the two less `overlap`, no further back than
`max_catchup`, and with neither from `lookback` ago. A gap is counted once, when a poll succeeds
(`jira_bridge_gaps_total`, `jira_bridge_gap_seconds_total`, and `jira_bridge_gaps_recovered_total`
unless `max_catchup` cut it short), continued from the store on start. JQL's relative minutes (`"-Nm"`) cover the
window on Jira's own clock, so the laptop's time zone and JQL's minute resolution do not matter.

### 13.5 People

| Account | Data Center | Cloud |
|---|---|---|
| What the bridge uses | `name` (the username); the map may list it or the user `key` | `accountId` |
| A bot | only if the map says `bot = true` | `accountType: app` (Automation for Jira), or the map |
| A stranger's source | `person.jira.<username>` | `person.jira.<account id>`, `bot.jira.<account id>` |
| Announced name | the map's, else Jira's `displayName` | the same |

A change with no author (automation, a workflow post function, an import) is Jira's own act,
published as `bot.jira` (`People.itself`: kind `program`, named `Jira`, no account), announced
like anyone else; on every site, since the map's `jira` key serves them all. The owner is the token's account
(`/myself`): the levels compare assignees and comment authors with it.

### 13.6 Two sites

`[bridges.jira_<name>]` is a second bridge: process and instrument `bridges.jira_<name>`,
its own `port` (required), its token from `JIRA_<NAME>_API_TOKEN` or
`~/.config/orchestra-bridges/jira_<name>-token`. Ids carry the site's host, so the two never
collide. Both announce people under the map's `jira` key, so one person on both sites is one
machine when the map lists both accounts.

### 13.7 Limits

- Only the owner's comment answers a comment.
- `jira.issue.updated` covers sprint, priority, labels, links and due date only.
- The snapshot fields are the issue as read, not as it was at the change.
- A Data Center user key the issue's JSON does not resolve is published as the key.
- A clock skew between the laptop and Jira larger than `overlap` could miss a change.
- `done` counts read every done issue's key, so they are refreshed only every `done_every`.
- Not yet run against a real Jira; the contract test (`tests/contract/test_jira_contract.py`) is
  ready for the first account.
