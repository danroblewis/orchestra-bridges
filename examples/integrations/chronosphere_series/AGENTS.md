# chronosphere_series: a poller for a Prometheus API (Prometheus, Chronosphere, Mimir) (kit `promql`)

You are a coding tool working on this directory. Read this first; it is the whole contract.

## What this is

`chronosphere_series.py` is a small program with two halves:

- **the poller** (`python chronosphere_series.py`): every few minutes (`machine.toml`, `[settings] every`; 5
  minutes by default) it reads what changed in a Prometheus API (Prometheus, Chronosphere, Mimir) and relays it as events on the project's
  Orchestra bus: none: a metrics store has no item events, so it has no poller and no `[[machines]]` entry (its `make_poll` does nothing);
- **the measurement** (`python chronosphere_series.py measure`): one read, printed as Prometheus text, which a
  `[[collect]]` entry runs and stores: one gauge per configured query, named by its `name`, with the answer's labels (aliases applied).

It was scaffolded by `orchestra bridges scaffold promql chronosphere_series` from the orchestra-bridges kits
(`orchestra_bridges.kits`: `CachedClient`, `Prometheus`, `Poller`, `runner`).

**The poller is a relay, never a machine** (Orchestra's rule 2, decision D19: the provider is never
the machine). Every event's `source` is who acted in a Prometheus API (Prometheus, Chronosphere, Mimir): `person.promql.<account>` for a
person, `bot.promql` for the system's own act (`actor_source`). The kit announces each actor once
(`orchestra.machine.started`, kind `person` or `program`) before their first event; the poller
never announces itself, never publishes as `chronosphere_series`, and `Poller.emit` refuses any other source.
The measurement is an instrument: it measures, never publishes.

Files: `machine.toml` (its settings, clearance and the events it publishes), `chronosphere_series.py` (what it
reads, keeps and publishes: `meta`/`keep_*`, `make_poll`, `measure`), `test_chronosphere_series.py` (its test
against the kit's fake), this file.

## What it may do

- **Read** a Prometheus API (Prometheus, Chronosphere, Mimir) through `CachedClient` only (rate-limited per host, retried with backoff,
  `Retry-After` honoured, cached on disk: only what `keep` returns). Keep `per_minute` low and
  `ttl` a little under `every`; read changes fresh (`ttl_s=0`) when a stale answer would hide one.
- **Publish** through `Poller.emit(type, subject, data, source=actor, stamp=...)` only: types
  `promql.<thing>.<verb>`, the item's own id as the subject, the actor as the source, and a stamp
  that changes when the item does (an updated time, a version), so a re-read never publishes twice.
- **Print series** in `measure` through `series.gauge` and `series.counter`, named for what they
  measure (`pagerduty_incidents_open`, never `chronosphere_series_metric`), with few label values.
- **Write** only under its state: the cursor in `<state>/kits/pollers/chronosphere_series/`, the cache in
  `<state>/kits/cache/` (the runner chooses these; don't hard-code others).

## What it must not do

- **Never text.** Publish, cache, log and print metadata only: ids, numbers, states, accounts,
  label names, times. Never a title, summary, description, comment, message body or a person's
  name. `keep`/`meta` in `chronosphere_series.py` decides what is cached and returned: keep it that strict.
  The test checks the fake's canary text appears in no output and no file.
- **Never write to a Prometheus API (Prometheus, Chronosphere, Mimir).** Read-only: no comments, no transitions, no acknowledgements.
- **Credentials by environment name only.** The token is read from `$PROMETHEUS_TOKEN` (the name is
  `[settings] token_env`), kept in memory, sent only to `base_url`'s host. Never put a token in
  `machine.toml`, `orchestra.toml`, a test, a log line or a commit; never print it or ask for it in
  a chat. If it is missing, say which variable to set, and stop.
- **The compliance check.** `clearance = "needs approval"` in `machine.toml` means the program
  reads only a fake on this machine (a loopback URL); it refuses a Prometheus API (Prometheus, Chronosphere, Mimir) itself before any
  request. Only the owner approves reading a company system (then `clearance = "approved"` and
  `data_class`); you never change it on your own.
- **No other writes.** Not to the repository's base branch (a change to the repository goes
  through a pull request), not to `orchestra.toml` by hand (the Supervisor changes configuration
  through `config_edit`, the overlay by default).

## How to test it

```bash
uv run python -m pytest examples/integrations/chronosphere_series/test_chronosphere_series.py -q
```

The test starts `FakePrometheus` from `orchestra_bridges.kits.fakes` on a random port, runs the poller
`--once` against it twice (a restart between), runs `measure`, and checks the events (types,
subjects, sources), the series, that nothing was published twice, and that no text or token left.
Add a case to the fake's fixture (or the test's own) for anything new you read. Try the real API
only after the owner's approval, and first with `--dry-run` (answers only from the cache).

## How to run it

By hand, once: `python examples/integrations/chronosphere_series/chronosphere_series.py --once` (in the project, `ORCHESTRA_REPO` set). For
good, the Supervisor adds two entries (through `config_edit`, the overlay by default): the poller's
process, and the instrument that stores its measurement:

```toml
[[collect]]                       # the instrument that stores its measurement
name = "chronosphere_series"
command = "python examples/integrations/chronosphere_series/chronosphere_series.py measure"
every = "60s"
timeout = "30s"
```

Then `orchestra check` lists the instrument, the Machines page shows the people and bots it relays
once they act, and the Instruments page shows `chronosphere_series`'s rounds and points.
