# chronosphere_series: a measurement of a Prometheus API (Prometheus, Chronosphere, Mimir) (kit `promql`)

You are a coding tool working on this directory. Read this first; it is the whole contract.

## What this is

Two ways to bring Chronosphere's series in, in order of preference:

- **the `[[sources]]` entry** (`sources.toml`): Orchestra reads the store in place at each discovery,
  under the entry's limits (`max_series`, `rate`, `max_requests_per_day`), with its label aliases
  (`[sources.labels]`); nothing runs and nothing is copied. `python check_source.py` checks it
  first (accepted, the store answers, the declared series come back, the aliases apply); the
  Supervisor then adds the two tables through `config_edit`, the overlay by default;
- **the fallback** (`python chronosphere_series.py measure`), when a series must be computed or
  stored here: each query in `machine.toml` once, printed as Prometheus text for a `[[collect]]`
  entry: one gauge per query, named by its `name`, with the answer's labels (aliases applied). A
  metrics store has no item events, so it has no poller and no `[[machines]]` entry.

The fallback was built with `orchestra bridges scaffold promql chronosphere_series` from the orchestra-bridges kits, then completed
(`orchestra_bridges.kits`: `CachedClient`, `Prometheus`, `Poller`, `runner`).

The measurement is an instrument: it measures, never publishes.

Files: `machine.toml` (its settings, clearance and the events it publishes), `chronosphere_series.py` (what it
reads and prints: `measure`), `test_chronosphere_series.py` (its test
against the kit's fake), this file.

## What it may do

- **Read** a Prometheus API (Prometheus, Chronosphere, Mimir) through `CachedClient` only (rate-limited per host, retried with backoff,
  `Retry-After` honoured, cached on disk: only what `keep` returns). Keep `per_minute` low and
  `ttl` a little under `every`; read changes fresh (`ttl_s=0`) when a stale answer would hide one.
- **Print series** in `measure` through `series.gauge` and `series.counter`, named for what they
  measure (`pagerduty_incidents_open`, never `chronosphere_series_metric`), with few label values.
- **Write** only under its state: the cache in
  `<state>/kits/cache/` (the runner chooses these; don't hard-code others).

## What it must not do

- **Never text.** Publish, cache, log and print metadata only: ids, numbers, states, accounts,
  label names, times. Never a title, summary, description, comment, message body or a person's
  name. `keep`/`meta` in `chronosphere_series.py` decides what is cached and returned: keep it that strict.
  The test checks the fake's canary text appears in no output and no file.
- **Never write to a Prometheus API (Prometheus, Chronosphere, Mimir).** Read-only: no comments, no transitions, no acknowledgements.
- **Credentials by environment name only.** The token is read from `$CHRONOSPHERE_TOKEN` (the name is
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

The test starts `FakePrometheus` from `orchestra_bridges.kits.fakes` on a random port, runs `measure`
against it, and checks the series and that no text or token left.
Add a case to the fake's fixture (or the test's own) for anything new you read. Try the real API
only after the owner's approval, and first with `--dry-run` (answers only from the cache).

## How to run it

By hand, once: `python examples/integrations/chronosphere_series/chronosphere_series.py measure` (in the project, `ORCHESTRA_REPO` set). For
good, the Supervisor adds one entry (through `config_edit`, the overlay by default): the
instrument that stores its measurement:

```toml
[[collect]]                       # the instrument that stores its measurement
name = "chronosphere_series"
command = "python examples/integrations/chronosphere_series/chronosphere_series.py measure"
every = "60s"
timeout = "30s"
```

Then the Instruments page lists `chronosphere_series` (its rounds, points and last error) (for a
`[[sources]]` store, `orchestra sources check` probes it).
