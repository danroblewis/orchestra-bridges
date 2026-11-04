# Bridges: design

The bridges are a **library** (the owner, 2026-11-01: "a library or framework so that AI agents
within my repo ... could write scripts in the target repo to do the metrics and event
collection"). They read no system themselves. A Supervisor or a coding tool scaffolds a collector
from a kit into the owner's repository (`.orchestra/machines/<name>/`), adapts and tests it there
against the kit's fake, and the owner's approval runs it from the project's own `[[machines]]` and
`[[collect]]` entries. Orchestra holds no bridge (its rule 1). The direct readers this repository
once ran (GitHub issues, GitHub pull requests, Jira, Slack pings) were retired on 2026-11-01; what
they knew is in the kit templates and the operator's guide, and their history in git.

## 1. The event a collector publishes

- **Type**: the system first, then what happened (`github.pr.opened`, `jira.issue.transitioned`),
  so systems never mix in Orchestra's counts by type. GitHub's are one vocabulary, the one the
  Workflows plugin's examples wait on (the `github` template's `AGENTS.md`).
- **Source**: who acted (§2); never the collector.
- **Subject**: the item, the system's own id (`acme/service#12`, `ABC-12`). Orchestra infers
  hand-offs from subjects.
- **Time and id**: the system's own; the id is `<name>:<type>:<key>@<stamp>`, the item's id and
  its change's stamp, so a re-read never publishes twice.
- **Data**: metadata only (numbers, states, accounts, label names, times). **Never text**: titles,
  bodies, comments and messages pass through memory only; every test checks a canary.

Orchestra reads the envelope and never the data (its rule 3).

## 2. People as machines; the collector is a relay

**The provider is never the machine** (Orchestra's rule 2, D19). A poller publishes only as the
person or bot who acted: `person.<system>.<account>`, `bot.<system>.<app>`, or `bot.<system>` for
the system's own act (`kits/poller.py` refuses any other source); an account the owner's identity
map (`[bridges] people`, `identity.py`) lists is `person.<key>` whichever system they acted in.
Each actor is announced once, just before their first event (`orchestra.machine.started`: `kind`
`person` or `program`, `mode` `ad hoc`, `package` `bridges`, the map's `name` and `team`), and
again when what is announced changes. The announcement also carries the poller's **notes**
(`kits/notes.py`): `fake` (relayed from a fake on this machine, added by the runner), `stand_in`
and `disabled` (`poller.toml`'s `[notes]`), one line each, so the actor's own card can say what
it is not. The poller never announces itself, never publishes as its own name, and is never
declared as a machine. Its measurement is an instrument (`[[collect]]`), which publishes nothing.

## 3. State

A poller keeps its cursor, the ids it emitted and its counters in
`<state>/kits/pollers/<name>/`, and a cache of only what it kept in `<state>/kits/cache/`. Nothing
else, and nothing in the repository.

**Resume and replays.** After each good poll the poller saves its cursor and `polled` (when that
poll began) in `cursor.json`; at start it reads both back, and the first poll after a restart reads
on from the saved cursor, so what was done while it was down is relayed once, then. An event is a
**replay** (`replayed: true` in its data, which a workflow's start refuses with
`"replayed": {"not": [true]}`) only when the poller had already passed it: by its id (in
`emitted.log`: skipped, never published twice) or by its time, before the last good poll before
this run began (`polled`, whole seconds; the first run, with no poll saved, its start less `every`,
so a catch-up window's history is marked). Never because it happened before the poller's (re)start:
an event done while the poller was down, seen first on the poll after the restart, is new and
published as new. (Until B4 the mark was the run's start less `every`: T2l's PR #59, opened at
06:18Z between the last poll and a 06:22Z restart, was counted as a replay.) A state saved before
`polled` was kept is read by the file's time.

## 4. Kits (`orchestra_bridges/kits/`)


Small parts the Supervisor, or the owner's coding tool, builds a collector from, and a scaffold
that writes one (Orchestra's `docs/INTEGRATING.md`). One module each:

- `client.py`, `CachedClient(base_url, token_env=None, token_file=None, per_minute=60,
  cache_dir=<state>/kits/cache, ttl_s=300)`: reads only, GET and a POST of a form, under one origin (any other
  method, body or origin is refused: a write is the outbox's; no project, no cache: an error),
  a disk TTL cache keyed by method, URL and body holding **only what the caller's `keep` returns**
  (a request without `keep` is never cached), a per-host token bucket (Orchestra's
  `orchestra.sources.limits.RateLimit`, in a file every process on the machine shares,
  `~/.orchestrator/stores/<host key>/kits.rate`), retries on 429, 5xx and dropped connections with
  a doubling backoff and `Retry-After` honoured (it holds the host's bucket), the token read once
  from a `chmod 600` file outside any repository or an environment variable (`secret.read_token`:
  a file inside the repository is refused) and never logged, keyed, cached or shown, `dry_run`,
  which answers from the cache only, `ca_file` (a CA bundle trusted beside the system's) and the
  environment's `HTTPS_PROXY`/`HTTP_PROXY`/`NO_PROXY` (a loopback host never proxied).
  `CachedClient.for_project` takes `[bridges.kits]`.
- `promql.py`, `Prometheus(client, aliases=None, max_series=500)`: `instant`, `range` and `series`
  against a Prometheus API as plain `Series(name, labels, points)`, labels renamed by `aliases`,
  an answer over `max_series` refused.
- `poller.py`, `Poller(name, every_s=300, state_dir=...)`: the interval loop (`run(poll)`, a clean
  stop on SIGTERM or SIGINT), the cursor in `cursor.json`, idempotent emission (`emit(type, subject,
  data, source, stamp=...)`, the id `<name>:<type>:<key>@<stamp>`, skipped when emitted before,
  across restarts: `emitted.log`), and `counter` totals kept with the cursor after each good poll,
  which the program's `measure` prints. **A poller is a relay** (§2, Orchestra's rule 2, D19): `emit` refuses any source but
  `person.…` and `bot.…` (`actor_source(system, account)`), and each actor is announced once
  (`orchestra.machine.started`, mode `ad hoc`, kind `person` or `program`, `listens_for`, `package`,
  and the poller's notes, §2) before their first event. The poller never announces itself or
  publishes as itself.
- `exposition.py`: the series' text (`Registry`), printed on standard output by `measure`.
- `runner.py`: the command line each scaffolded poller shares: `python <name>.py` polls,
  `python <name>.py measure` prints one measurement for its `[[collect]]` entry (an instrument,
  which publishes nothing); both run the compliance check first. The poller run for good (no
  `--once`/`--rounds`) and not cleared waits instead of exiting: one `waiting for approval` line,
  nothing read, `poller.toml` re-read on its interval until approved (so an early `[[machines]]`
  entry does not crash-loop). `--set KEY=VALUE` gives one run the kit's own settings (a test's
  scope); never the URL or the credentials. A measurement-only program (`promql`) passes no
  `make_poll`, and refuses to poll. **Against a fake on this machine** (a loopback URL) it reads no
  credential: it sends `[settings] fake_token`/`fake_user` or `FAKE_TOKEN`/`FAKE_USER`, keeps no
  rate, and adds the note `fake`. **Exit codes**, the one statement of them: 0 done (a poll run
  that ended, a stop while waiting, a measurement printed); 1 a measurement whose read failed; 2
  refused before any read (`poller.toml` unreadable, no project, not cleared for `--once`,
  `--rounds` or `measure`, a missing token, a poll asked of a measurement-only program).
- `testing.py`: what a scaffolded test reads from its `poller.toml` (the token's variable, the
  scope, the placeholders its fake checks), so it passes after the owner's values are set.
- `notes.py`: `fake`, `stand_in`, `disabled`, one line each, on each actor's announcement (§2).
- `integrations.py`, `orchestra bridges integrations [--json]`: read-only, the kits and the machines
  scaffolded under the three roots, with kit, clearance, data class, the token's variable NAME,
  what schedules the poller (`[[machines]]`, `every`: a Workflows `workflows.every.<item>` whose role is
  its name with a `machine.json` beside it, or `no`), whether its `[[collect]]` entry is configured,
  and the `# guess` marks left.
- `machinefile.py`: a poller's `poller.toml` (`[settings]`: `base_url`, `token_env`, `every`,
  `per_minute`, `ttl` and the kit's own keys; never a token) and its **clearance**: until
  `clearance = "approved"` (the owner's say), it reads only a fake on a loopback host.
- `fakes/`: stdlib HTTP fakes of GitHub (pulls, issues, issue events, reviews, merge, check runs,
  commit status; labels by POST, PUT and DELETE), Jira in both editions (`edition=`: Data Center's `/rest/api/2/search` paged by `startAt`, a
  personal access token as Bearer and users by `name`; Cloud's `/rest/api/3/search/jql` paged by
  `nextPageToken`, HTTP Basic and account ids; `serverInfo` with its `deploymentType`; the
  changelog, an issue, transitions), Slack (`conversations.history`, `users.info`), Discord (a channel's messages, threads, reactions, the guild's channels; its rate-limit headers), PagerDuty (incidents, an incident)
  and a Prometheus API (`query`, `query_range`, `series`), each on a random port, seeded from a JSON
  fixture, checking a token, recording requests and failing on request; a canary in every text.
  Their base, `FakeServer` (exported), is for a fake of a new system: a subclass implements
  `route(method, path, query, body)`; the `http` template's test shows one.
- `scaffold.py`, `orchestra bridges scaffold <kind> <name> [--root repo|state|overlay]`: a collector
  from a template (`github`, `jira`, `slack`, `discord`, `pagerduty`; `http`, any JSON API no kit knows: a list endpoint, items
  mapped to events by a key and a change stamp, one gauge; `promql`, a measurement only) as
  `<root>/machines/<name>/{poller.toml, <name>.py, test_<name>.py, AGENTS.md}`: `.orchestra/` in
  the repository by default, the state directory, or beside the private overlay. It writes no
  configuration: it prints the test command (Orchestra's own Python) and the `[[machines]]` and
  `[[collect]]` entries for `config_edit` (`python <path from the repository's root>`, `every` the
  template's 5 minutes). `examples/integrations/scaffolded/` is what each kit writes.
- `settings.py`: `[bridges.kits]` (`cache_dir`, `ttl`, `per_minute`, `hosts`, `tokens`: system to
  environment variable NAME, `ca_file`), checked by the plugin's `check`.

## 5. Running

`plugin.py` is the `orchestra.plugins` entry point `bridges`. It declares **no process, no
instrument and no machine**: only its guide (`guide.md`, `/api/guides`) and the check of
`[bridges]` (`check.py`: `[bridges.kits]`, `people`; a retired reader's section is refused with
the kit that replaces it). A scaffolded collector runs from the project's own configuration,
written by the Supervisor's `approve_integration` once the owner approved: a `[[machines]]` entry
(`restart = "always"`) for its poller, which the launcher knows only as a process, and a
`[[collect]]` entry for its measurement. `orchestra bridges scaffold` and `orchestra bridges
integrations` are the plugin's commands (`__main__.py`).

`orchestra_bridges/rabbitmq/preset.toml` is configuration for Orchestra's own `[[sources]]`
(counted traffic over RabbitMQ's per-queue counts), not a collector; its test rig is
`tests/rabbitmq_rig/` (README.md, "RabbitMQ").

## 6. Tests

- `tests/unit`: the kits (client: cache, rate, retries, the read-only rule, the token, the proxy
  and a CA file; poller: the relay rule, idempotence, notes; fakes; scaffold: every kit's own test
  passes against its fake, with the owner's values too, and Jira Cloud's; a real token never
  reaches a fake), the plugin and its check, the guide, the identity map, the RabbitMQ preset, and
  `test_scaffolded_examples.py` (the committed references are what the templates write).
- `examples/integrations`: each example's and each scaffolded reference's own test, against the fakes.
- `tests/e2e/test_integrations_run.py`: `orchestra run` keeps three pollers going as `[[machines]]`
  against their fakes; the Machines page shows their actors, never the pollers.
- `tests/contract/test_rabbitmq_live.py` (opt in): the preset against the rig's real store.

## 7. The scripted team (test tooling)

The owner, 2026-11-03: "When I start using this at work, we won't be doing any write-access to Jira
or Slack, so to test it you will need to create and modify Jira tickets and send Discord messages to
test that the read-only features are working." `python -m orchestra_bridges.team`
(`orchestra_bridges/team/`) plays a team's working day against a Jira test project and Discord test
channels, so the read-only pollers relay something real. It is the package's one writer, and it is
test tooling: run by hand, never by the plugin; **never a machine** (nothing declares it, it
announces and publishes nothing; the pollers relay its acts as the accounts it used).

- **The day** (`day.py`): eight rounds, drawn from `--seed`: five tickets created (summaries
  `[orchestra live test] …`, labels `orchestra-live-test` and `orchestra-live-team`), assigned to the
  project's assignable people, moved by stage (start, review, done) through the transitions they
  have, some commented on; in the channels a status ask at each end of the day, questions answered
  inline, an ad-hoc request that becomes a ticket, an incident alert answered and checked (✅).
  `--rhythm` is how long the day takes; `--once` plays one round (the next of an unfinished day).
- **The ledger** (`--ledger`, JSON): per day the tickets' keys and ids, the messages' channels and
  ids, and what was done when; never text. Written after every write.
- **The close** (`--close`): every ticket the ledger holds and nothing closed, walked to a done
  status with `--resolution` when the screen takes one; only a ticket with the team's label and
  prefix. Nothing is ever deleted, edited away or unsent.
- **Limits**: a write at most every 2 seconds across both systems (`wire.py`), a 429 waited out;
  credentials by file reference only (`secret.read_token`), none to a fake on this machine;
  `--dry-run` writes nothing (it reads the people, the channels' names and the tickets' status).
- **Tests**: `tests/unit/test_team.py`, against `FakeJira` (Cloud) and `FakeDiscord`, which accept
  the team's writes (an issue, an assignee, a comment, a resolution; a message, a reaction).

## 8. Not built yet

Webhooks; Confluence and Drive kits; the Pub/Sub preset; a Slack kit for the owner's pings
(search with a user token: what the retired Slack reader did) and a posting fake.
