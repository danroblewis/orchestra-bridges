# promql_example: a measurement of a Prometheus API (Prometheus, Chronosphere, Mimir) (kit `promql`)

You are a coding tool working on this directory. Read this first; it is the whole contract.

## What this is

`promql_example.py` is a measurement only (`python promql_example.py measure`): one read, printed as Prometheus
text, which a `[[collect]]` entry runs and stores: one gauge per configured query, named by its `name`, with the answer's labels (aliases applied). It has no poller: a Prometheus API (Prometheus, Chronosphere, Mimir) has no
item events to relay, so `python promql_example.py` without `measure` refuses (exit 2).

It was scaffolded by `orchestra bridges scaffold promql promql_example` from the orchestra-bridges kits
(`orchestra_bridges.kits`: `CachedClient`, `Prometheus`, `Poller`, `runner`).

The measurement is an instrument: it measures, never publishes.

**Values marked `# guess` in `machine.toml`** (the site, the scope: a JQL, channels, queries, an
API's shape) are the template's placeholders: ask the owner for each, or read it from the API's
own docs, and remove the mark once it is theirs. `orchestra bridges integrations` counts the guesses
left.

Files: `machine.toml` (its settings, clearance), `promql_example.py` (what it
reads, keeps and prints: `measure`), `test_promql_example.py` (its test
against the kit's fake: it reads the token's variable and the scope from `machine.toml`, so it
still passes once the owner's values are in), this file.

## What it may do

- **Read** a Prometheus API (Prometheus, Chronosphere, Mimir) through `CachedClient` only (rate-limited per host, retried with backoff,
  `Retry-After` honoured, cached on disk: only what `keep` returns). Keep `per_minute` low and
  `ttl` a little under `every`; read changes fresh (`ttl_s=0`) when a stale answer would hide one.
- **Print series** in `measure` through `series.gauge` and `series.counter`, named for what they
  measure (`pagerduty_incidents_open`, never `promql_example_metric`), with few label values.
- **Write** only under its state: the cache in
  `<state>/kits/cache/` (the runner chooses these; don't hard-code others).

## What it must not do

- **Never text.** Publish, cache, log and print metadata only: ids, numbers, states, accounts,
  label names, times. Never a title, summary, description, comment, message body or a person's
  name. `keep`/`meta` in `promql_example.py` decides what is cached and returned: keep it that strict.
  The test checks the fake's canary text appears in no output and no file.
- **Never write to a Prometheus API (Prometheus, Chronosphere, Mimir).** Read-only: no comments, no transitions, no acknowledgements.
- **Credentials by reference only, a file first.** The first answer is a file: `[settings]
  token_file` (and `user_file` for an account), a `chmod 600` file outside any repository, which
  only this program reads. The second is an environment variable's NAME, `[settings] token_env`
  (`$PROMETHEUS_TOKEN` here; `user_env`): every program `orchestra run` starts inherits its
  environment, so a token there reaches them all. Either way the token is kept in memory and sent
  only to `base_url`'s host. Never put a token in `machine.toml`, `orchestra.toml`, a test, a log
  line or a commit; never print it or ask for it in a chat. If it is missing, say which file or
  variable the owner should provide, and stop. How it is sent is `auth_format` in
  `promql_example.py`'s `runner.main(...)`: `"Bearer {token}"` (the default: Jira Data Center, Slack,
  most APIs), `"Token token={token}"` (PagerDuty), `"Basic {token}"` (Basic auth: the variable
  holds base64 of `user:api_token`, as Jenkins wants), `"Basic {basic}"` (an account and a token
  kept apart: `user_env` or `user_file` names the account, as Jira Cloud wants), or a header of
  the API's own through `headers=` with `auth_format` for `Authorization`.
- **A fake never gets a credential.** Against a loopback URL the runner reads no token and no
  account: it sends `[settings] fake_token` (and `fake_user`), or the kit's placeholders, and the
  test starts the fake with the same (`testing.fake_token`). Never pass a real token to a fake.
- **Behind a corporate proxy**: `HTTPS_PROXY` and `NO_PROXY` in `orchestra run`'s environment are
  honoured (a fake on this machine never goes through a proxy); a proxy that re-signs TLS needs its
  CA: `[settings] ca_file` (a PEM bundle, trusted beside the system's), or `[bridges.kits] ca_file`
  for every collector.
- **The compliance check.** `clearance = "needs approval"` in `machine.toml` means the program
  reads only a fake on this machine (a loopback URL); it refuses a Prometheus API (Prometheus, Chronosphere, Mimir) itself before any
  request. Only the owner approves reading a company system (then `clearance = "approved"` and
  `data_class`); you never change it on your own.
- **No other writes.** Not to the repository's base branch (a change to the repository goes
  through a pull request), not to `orchestra.toml` by hand (the Supervisor changes configuration
  through `config_edit`, the overlay by default).

## How to test it

```bash
python -m pytest .orchestra/machines/promql_example/test_promql_example.py -q
```

with Orchestra's own Python (the one `orchestra` runs in: the scaffold printed its full path),
which has the kits and pytest; `uv run` in this repository uses the repository's own environment,
which may have neither.

The test starts `FakePrometheus` from `orchestra_bridges.kits.fakes` on a random port, runs `measure`
against it, and checks the series and that no text or token left.
The fake holds the scope the test gives the run (`--set`, from `machine.toml` where it can: the
JQL's first project, the first channel), so the owner's own scope needs no change to the test.
Add a case to the fake's fixture (or the test's own) for anything new you read. Try the real API
only after the owner's approval, and first with `--dry-run` (answers only from the cache).

## How to run it

By hand, once: `python .orchestra/machines/promql_example/promql_example.py measure` (in the project, `ORCHESTRA_REPO` set). For
good, after the owner's approval (scaffold, test against the fake, `clearance` and the token's
variable name set, then the entries), the Supervisor adds one entry (through `config_edit`, the overlay by default): the
instrument that stores its measurement:

```toml
[[collect]]                       # the instrument that stores its measurement
name = "promql_example"
command = "python .orchestra/machines/promql_example/promql_example.py measure"
every = "5m"
timeout = "60s"
```

Then the Instruments page lists `promql_example` (its rounds, points and last error) (for a `[[sources]]` store, `orchestra sources check` probes it).
