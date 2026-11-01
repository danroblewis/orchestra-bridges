# slack_example: a poller for Slack (kit `slack`)

You are a coding tool working on this directory. Read this first; it is the whole contract.

## What this is

`slack_example.py` is a small program with two halves:

- **the poller** (`python slack_example.py`): every few minutes (`machine.toml`, `[settings] every`; 5
  minutes by default) it reads what changed in Slack and relays it as events on the project's
  Orchestra bus: `slack.message.posted` (subject: `<channel>/<thread ts>`; source: `person.slack.<member id>` or `bot.slack.<id>`; data: channel, reply or not, bot or not);
- **the measurement** (`python slack_example.py measure`): one read, printed as Prometheus text, which a
  `[[collect]]` entry runs and stores: `slack_channel_messages_last_hour{channel}`.

It was scaffolded by `orchestra bridges scaffold slack slack_example` from the orchestra-bridges kits
(`orchestra_bridges.kits`: `CachedClient`, `Prometheus`, `Poller`, `runner`).

**The poller is a relay, never a machine** (Orchestra's rule 2, decision D19: the provider is never
the machine). Every event's `source` is who acted in Slack: `person.slack.<account>` for a
person, `bot.slack` for the system's own act (`actor_source`), or the person the project's
identity map names (`[bridges] people`). The kit announces each actor once
(`orchestra.machine.started`, kind `person` or `program`) before their first event; the poller
never announces itself, never publishes as `slack_example`, and `Poller.emit` refuses any other source.
The measurement is an instrument: it measures, never publishes.

**Values marked `# guess` in `machine.toml`** (the site, the scope: a JQL, channels, queries, an
API's shape) are the template's placeholders: ask the owner for each, or read it from the API's
own docs, and remove the mark once it is theirs. `orchestra bridges integrations` counts the guesses
left.

Files: `machine.toml` (its settings, clearance and the events it publishes), `slack_example.py` (what it
reads, keeps and publishes: `meta`/`keep_*`, `make_poll`, `measure`), `test_slack_example.py` (its test
against the kit's fake: it reads the token's variable and the scope from `machine.toml`, so it
still passes once the owner's values are in), this file.

## What it may do

- **Read** Slack through `CachedClient` only (rate-limited per host, retried with backoff,
  `Retry-After` honoured, cached on disk: only what `keep` returns). Keep `per_minute` low and
  `ttl` a little under `every`; read changes fresh (`ttl_s=0`) when a stale answer would hide one.
- **Publish** through `Poller.emit(type, subject, data, source=actor, stamp=...)` only: types
  `slack.<thing>.<verb>`, the item's own id as the subject, the actor as the source, and a stamp
  that changes when the item does (an updated time, a version), so a re-read never publishes twice.
- **Print series** in `measure` through `series.gauge` and `series.counter`, named for what they
  measure (`pagerduty_incidents_open`, never `slack_example_metric`), with few label values.
- **Write** only under its state: the cursor in `<state>/kits/pollers/slack_example/`, the cache in
  `<state>/kits/cache/` (the runner chooses these; don't hard-code others).

## What it must not do

- **Never text.** Publish, cache, log and print metadata only: ids, numbers, states, accounts,
  label names, times. Never a title, summary, description, comment, message body or a person's
  name. `keep`/`meta` in `slack_example.py` decides what is cached and returned: keep it that strict.
  The test checks the fake's canary text appears in no output and no file.
- **Never write to Slack.** Read-only: no comments, no transitions, no acknowledgements.
- **Credentials by reference only, a file first.** The first answer is a file: `[settings]
  token_file` (and `user_file` for an account), a `chmod 600` file outside any repository, which
  only this program reads. The second is an environment variable's NAME, `[settings] token_env`
  (`$SLACK_TOKEN` here; `user_env`): every program `orchestra run` starts inherits its
  environment, so a token there reaches them all. Either way the token is kept in memory and sent
  only to `base_url`'s host. Never put a token in `machine.toml`, `orchestra.toml`, a test, a log
  line or a commit; never print it or ask for it in a chat. If it is missing, say which file or
  variable the owner should provide, and stop. How it is sent is `auth_format` in
  `slack_example.py`'s `runner.main(...)`: `"Bearer {token}"` (the default: Jira Data Center, Slack,
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
  reads only a fake on this machine (a loopback URL); it refuses Slack itself before any
  request. Only the owner approves reading a company system (then `clearance = "approved"` and
  `data_class`); you never change it on your own. Until then the poller that runs for good
  (no `--once`) does not exit: it logs `waiting for approval` once, reads nothing, and re-reads
  `machine.toml` on its interval, polling as soon as it says `approved`.
- **No other writes.** Not to the repository's base branch (a change to the repository goes
  through a pull request), not to `orchestra.toml` by hand (the Supervisor changes configuration
  through `config_edit`, the overlay by default).

## How to test it

```bash
python -m pytest .orchestra/machines/slack_example/test_slack_example.py -q
```

with Orchestra's own Python (the one `orchestra` runs in: the scaffold printed its full path),
which has the kits and pytest; `uv run` in this repository uses the repository's own environment,
which may have neither.

The test starts `FakeSlack` from `orchestra_bridges.kits.fakes` on a random port, runs the poller
`--once` against it twice (a restart between), runs `measure`, and checks the events (types,
subjects, sources), the series, that nothing was published twice, and that no text or token left.
The fake holds the scope the test gives the run (`--set`, from `machine.toml` where it can: the
JQL's first project, the first channel), so the owner's own scope needs no change to the test.
Add a case to the fake's fixture (or the test's own) for anything new you read. Try the real API
only after the owner's approval, and first with `--dry-run` (answers only from the cache).

## How to run it

By hand, once: `python .orchestra/machines/slack_example/slack_example.py --once` (in the project, `ORCHESTRA_REPO` set). For
good, after the owner's approval (scaffold, test against the fake, `clearance` and the token's
variable name set, then the entries), the Supervisor adds two entries (through `config_edit`, the overlay by default): the poller's
process, and the instrument that stores its measurement:

```toml
[[machines]]                      # the poller's process (a relay: its actors are the machines)
name = "slack_example"
command = "python .orchestra/machines/slack_example/slack_example.py"
restart = "always"

[[collect]]                       # the instrument that stores its measurement
name = "slack_example"
command = "python .orchestra/machines/slack_example/slack_example.py measure"
every = "5m"
timeout = "60s"
```

Against a fake, each actor's announcement says so (`fake`, shown with the machine), and a
`[notes]` table in `machine.toml` adds what else the owner should know on each card: `stand_in`
(it acts for someone who cannot here), `disabled` (it runs and does nothing, and why). One line
each, the program's own words, never text from Slack.

Then the Instruments page lists `slack_example` (its rounds, points and last error), `orchestra events info`
shows the events arriving on the bus, and the Machines page shows the people and bots it relays
once they act (for a `[[sources]]` store, `orchestra sources check` probes it).
