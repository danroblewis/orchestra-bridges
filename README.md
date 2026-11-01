# orchestra-bridges

A library for bringing an outside system into an [Orchestra](https://github.com/danroblewis/orchestra)
project: the parts from which a Supervisor or a coding tool writes, **in the owner's repository**,
the scripts that relay what people did in GitHub, Jira, Slack or PagerDuty as events on the
project's bus and measure what is there for its metrics store (the owner, 2026-11-01: "a library or
framework so that AI agents within my repo ... could write scripts in the target repo to do the
metrics and event collection").

The plugin reads no system itself. It gives:

- **the kits** (`orchestra_bridges.kits`): `CachedClient` (read-only HTTP: a disk cache of only what
  the script keeps, a per-host rate limit shared by every process, retries with `Retry-After`, the
  token in memory, `ca_file` and `HTTPS_PROXY`/`NO_PROXY` for a corporate network), `Poller` (the
  interval loop, the cursor, each event published once, the relay rule), `Prometheus`, and the
  runner every scaffolded script shares;
- **fakes** of GitHub, Jira (Data Center and Cloud), Slack, PagerDuty and a Prometheus API, in
  their real shapes, with a canary in every text; `FakeServer` for any other API;
- **templates and a scaffold**: `orchestra bridges scaffold <github|jira|slack|pagerduty|promql|http> <name>`;
- **the clearance gate**: a scaffolded script reads only a fake on this machine until the owner
  approves reading the real system;
- **`orchestra bridges integrations`**: the scaffolded collectors, their clearance and their
  token's whereabouts, read-only;
- **the operator's guide** (`orchestra_bridges/guide.md`), served to the Supervisor, and the
  check of `[bridges.kits]` that `config_edit` runs.

The Supervisor's `integrate_system` and `approve_integration` tools (orchestra-agents) and
Orchestra's `docs/INTEGRATING.md` are the procedure; this repository is what they build from.

## A collector, from scaffold to running

```bash
orchestra bridges scaffold jira jira_poll        # into .orchestra/machines/jira_poll/ (--root state|overlay)
# prints the test command, with Orchestra's own Python (which has the kits and pytest):
/path/to/orchestra/python -m pytest .orchestra/machines/jira_poll/test_jira_poll.py -q
orchestra bridges integrations                   # each one's clearance, token reference, entries, guesses left
```

It writes `poller.toml` (the settings: the site, the scope, where the token is by reference,
`every = "5m"`; the clearance; the events it publishes), `jira_poll.py` (what it reads, keeps and
publishes), `test_jira_poll.py` (against the kit's fake) and an `AGENTS.md` with the rules for
whoever edits it. Values the template guessed are marked `# guess`: they are the owner's to give.
Once the owner approved, `approve_integration` writes the clearance and the credentials' references
into `poller.toml` and two entries into the owner's overlay:

```toml
[[machines]]                      # the poller's process (a relay: its actors are the machines)
name = "jira_poll"
command = "python .orchestra/machines/jira_poll/jira_poll.py"
restart = "always"

[[collect]]                       # the instrument that stores its measurement
name = "jira_poll"
command = "python .orchestra/machines/jira_poll/jira_poll.py measure"
every = "5m"
timeout = "60s"
```

`examples/integrations/scaffolded/machines/` holds exactly what each kit writes; the four completed
examples beside it (`jira_poller`, `slack_watcher`, `pagerduty_alerts`, `chronosphere_series`) show
a scaffold taken further.

## The rules a collector keeps

- **A relay, never a machine** (Orchestra's rule 2): every event's source is who acted
  (`person.<system>.<account>`, `bot.<system>.<app>`, `bot.<system>`, or `person.<key>` when the
  identity map, `[bridges] people`, lists the account), each announced once; the poller never
  announces itself or publishes as its own name. Its measurement is an instrument.
- **Metadata only**: ids, states, accounts, label names, times; never a title, body, comment or
  message. The cache holds only what `keep` returns; every test checks the canary leaves nowhere.
- **Read only**: the client sends GET (or a form POST that asks a question) and refuses anything
  else. A write to the other system (a label, a transition, a Slack post) is an Outbox draft that
  the owner's approval settings govern.
- **Credentials by reference**: a `chmod 600` file outside any repository (`token_file`,
  `user_file`) first, an environment variable's name (`token_env`) second; never a value in a
  file of ours. **A fake never gets one**: against a loopback URL the runner sends placeholders
  (`fake_token`, `fake_user`), and each actor's announcement says it was relayed from a fake.
- **Clearance**: `clearance = "needs approval"` reads only a fake; the owner's approval writes
  `"approved"` and the data class.

## The kits

| Kit | Relays | Measures |
|---|---|---|
| `github` | `github.pr.opened`, `.synchronize`, `.labeled`, `.unlabeled`, `.review_requested`, `.approved`, `.changes_requested`, `.commented`, `.merged`, `.closed`, `.reopened`, `github.checks.completed`, `github.issue.opened`, `.labeled`, `.unlabeled`, `.closed`, `.reopened` (the vocabulary the Workflows plugin's examples wait on) | `github_issues_open`, `github_prs_open{review}` |
| `jira` | `jira.issue.created`, `.assigned`, `.transitioned`; `flavour` = `datacenter` (REST v2, a personal access token) or `cloud` (REST v3 `/search/jql`, the account's email and an API token), `auto` by the site's host | `jira_issues{status, category}` |
| `slack` | `slack.message.posted` in the channels named | `slack_channel_messages_last_hour{channel}` |
| `pagerduty` | `pagerduty.incident.triggered`, `.acknowledged`, `.resolved` | `pagerduty_incidents_open` |
| `promql` | nothing (a store has no item events): prefer Orchestra's `[[sources]]` | a few PromQL answers as gauges |
| `http` | `<system>.<thing>.<state>`, a guess at an API no kit knows | `<system>_<thing>s{state}` |

```toml
[bridges.kits]                     # optional: every collector's defaults
per_minute = 60                    # requests a minute per host
hosts = { "jira.acme.com" = 20 }
tokens = { jira = "JIRA_API_TOKEN" }   # environment variable NAMES, never a token
ttl = "4m"
ca_file = "~/.config/orchestra-bridges/corporate-ca.pem"
```

The direct readers this package once ran (`[bridges.github]`, `[bridges.github_prs]`,
`[bridges.jira]`, `[bridges.slack]`) were retired on 2026-11-01; the plugin's check refuses their
sections and names the kit that replaces each.

## RabbitMQ: a preset and a test rig (counted traffic, not a bridge)

Some buses carry too much, or data that is not ours, to read message by message. Orchestra reads
them by **counts**: what each producer put into a channel and each consumer took from it, and what
waits (Orchestra's `docs/proposals/event-sources.md` §1–§3, its SPEC §4.4). Nothing here
publishes events; Orchestra reads the counts from a Prometheus-compatible store.

- `orchestra_bridges/rabbitmq/preset.toml`: the series definitions for RabbitMQ's own per-queue
  counts (the `rabbitmq_prometheus` plugin's `/metrics/detailed`): publishes per exchange,
  acknowledgements, backlog and consumers per queue. Exchanges are channels; a queue named
  `<consumer>.<anything>` is that consumer's. RabbitMQ never names publishers, so a project names
  them in `[sources.producers]`, or they are drawn unattributed. Name the preset from a project's
  `[[sources]]` table (`preset = "..."`, the path `python -c "import importlib.resources as r; print(r.files('orchestra_bridges') / 'rabbitmq/preset.toml')"`
  prints); `tests/rabbitmq_rig/orchestra.toml` is an example.
- `tests/rabbitmq_rig/fleet.py` (a test system, not part of the package): a simulated fleet for testing it. `orders-api`,
  `payments-api` and `web` publish into three fanout exchanges; `billing` acknowledges in batches
  every 20 s, `shipping` and `notifier` keep up, and `analytics` is too slow at peak, so its
  backlog grows and drains with a 20-minute simulated day.
- `tests/rabbitmq_rig/rig.sh up|down|status`: a RabbitMQ (`orchestra-test-rabbit`:
  AMQP on 127.0.0.1:5682, management on 15682, metrics on 15702), a VictoriaMetrics that scrapes it
  every 5 s (`orchestra-test-rabbit-vm`, 127.0.0.1:8441) and the fleet, whose pid and log are in
  `~/.local/state/orchestra-rabbit-rig/`. The fleet needs only `pika`
  (`uv run --no-project --with pika`).

What the rig shows, in a project made from `tests/rabbitmq_rig/orchestra.toml`: the Machines page draws the seven
services as machines known by counts, the three exchanges as channels, double lines with each
rate, and the two unnamed publishers as unattributed. Discovery finds each slow or batched
consumer's backlog as a stock filled by its exchange's publishes and drained by its own acks, the
orders exchange's publishes as one flow with a copy into each other backlog it fills. A consumer
that keeps up (shipping, notifier) leaves a backlog too small to be a stock.

## Develop

```sh
uv sync --extra dev
uv run python -m pytest -q          # unit and e2e tests, the examples and the scaffolded references, against the fakes
uv run ruff check .
```

After changing a template, regenerate the references:
`ORCHESTRA_BRIDGES_REGENERATE=1 uv run python -m pytest tests/unit/test_scaffolded_examples.py`.
Read `SPEC.md` for the design and `AGENTS.md` for the rules.

## Developing against a local Orchestra

`pyproject.toml` requires `orchestra>=1.0.0a1,<1.0.0rc0` and takes it from GitHub's `main`
(`[tool.uv] override-dependencies`); `uv.lock` pins the commit it resolved to, so nothing needs
cloning beside this repository. The override is this repository's own: installed as a plugin, it
gets the Orchestra it is installed with. `uv lock --upgrade-package orchestra` moves the pin to the
newest `main` (then `uv sync --extra dev`).

To run against an Orchestra checkout instead, with its uncommitted changes, lay it over this
environment for one command:

```bash
uv run --with-editable ~/orchestra_2 python -m pytest -q      # the path to your Orchestra checkout
uv run --with-editable ~/orchestra_2 orchestra web
```

Nothing is written: `.venv`, `pyproject.toml` and `uv.lock` keep GitHub's Orchestra, and a plain
`uv run` uses it again. Inside the command, `sys.executable` and the `orchestra` beside it import
the checkout, so the processes the tests and the host start do too.

For something that runs `.venv/bin/orchestra` directly, install the checkout into `.venv` and keep
uv from syncing it away:

```bash
uv pip install -e ~/orchestra_2
uv run --no-sync python -m pytest -q          # or export UV_NO_SYNC=1
uv sync --extra dev                            # back to the locked Orchestra from GitHub
```

A plain `uv run` or `uv sync` also puts the locked Orchestra back.

To keep a checkout as this environment's Orchestra, point `pyproject.toml` at it,
then `uv sync --extra dev`; leave that change, and the `uv.lock` it rewrites, out of commits
(`git checkout pyproject.toml uv.lock`, then `uv sync --extra dev`, goes back):

```toml
[tool.uv]
override-dependencies = ["orchestra[sd]"]
sources = { orchestra = { path = "../orchestra_2", editable = true } }   # the path to your Orchestra checkout
```
