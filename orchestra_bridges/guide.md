# Bridges: an operator's guide

For the AI operator (the Supervisor) that brings an outside system into a user's project. Decision
knowledge, not a procedure: what the plugin gives you, where a collector lives, how to choose a
kit and its settings, what goes wrong, and which choices are the human's. The steps themselves
are your `integrate_system` and `approve_integration` tools and Orchestra's `docs/INTEGRATING.md`.

## What the plugin is

A library, never a reader (the owner, 2026-11-01: "a library or framework so that AI agents within
my repo could write scripts in the target repo to do the metrics and event collection"). It reads
no system itself, runs no process and declares no instrument or machine. It gives you:

- **the kits** (`orchestra_bridges.kits`): `CachedClient` (read-only HTTP with a disk cache of only
  what the script keeps, a per-host rate limit shared by every process, retries, the token in
  memory), `Poller` (the interval loop, the cursor, each event published once, the series for a
  `[[collect]]`), `Prometheus`, and the runner every scaffolded script shares;
- **the fakes** (`kits.fakes`: `FakeGitHub`, `FakeJira` in both editions, `FakeSlack`,
  `FakeDiscord`, `FakePagerDuty`, `FakePrometheus`, and `FakeServer` for a system no kit knows), in the systems'
  real shapes, with a canary in every text so a test shows none of it leaves;
- **the templates and the scaffold**: `orchestra bridges scaffold <kit> <name>` writes a collector
  (`poller.toml`, `<name>.py`, `test_<name>.py`, `AGENTS.md`) that a coding tool adapts.
  `examples/integrations/scaffolded/` in this repository is exactly what each kit writes;
- **the clearance gate**: a scaffolded script reads only a fake on this machine until its
  `poller.toml` says the owner approved;
- **`orchestra bridges integrations`**: every scaffolded collector, its clearance, where its token
  is (by name), whether its entries are written and the guesses left. Read-only.

The kits are installed only if the `orchestra-bridges` package is in Orchestra's environment
(installing it is the human's). The direct readers this plugin once had (`[bridges.github]`,
`[bridges.github_prs]`, `[bridges.jira]`, `[bridges.slack]`) were retired on 2026-11-01: a section
of theirs is refused by the plugin's check, with the kit that replaces it.

## A poller is a relay, never a machine

A scaffolded poller publishes what people and bots did in the other system, as them:
`person.<system>.<account>`, `bot.<system>.<app>`, or `bot.<system>` for the system's own act.
It announces each of them once and never announces itself or publishes as its own name; those
actors appear on the Machines page once they act. Its measurement is an instrument (the
`[[collect]]` entry that runs `<name>.py measure`). So never declare the poller as a machine, and
never schedule it as a role or a workflow item: the system itself is what you declare (managed
elsewhere, heard as `bot.<system>`), with its expected effect on the stock it fills.

## Where a collector lives

- **The owner's repository**, `.orchestra/machines/<name>/` (the scaffold's default, `--root repo`;
  `integrate_system`'s `root: repository`). A coding tool or the Coordinator's worker edits it
  there and a pull request lands it, like any other code. The scaffold writes files and never
  commits.
- **The state directory**, `~/.orchestrator/<key>/machines/<name>/` (`--root state`), only when
  the owner says their repository must stay clean. It is then nobody's code review: say so.
- **Beside the private overlay** (`--root overlay`), only when the overlay lives in a private
  repository of the owner's.

An existing directory is never overwritten: pick another name, or remove the old one on the
owner's word.

## Choosing a kit

| The system | Kit | What the template relays and measures |
|---|---|---|
| GitHub issues and pull requests | `github` | `github.pr.opened`, `.synchronize`, `.labeled`, `.unlabeled`, `.review_requested`, `.approved`, `.changes_requested`, `.commented`, `.merged` (`.closed`, `.reopened`), `github.checks.completed`, `github.issue.opened`, `.labeled`, `.closed`, `.reopened`; `github_issues_open`, `github_prs_open{review}` |
| Jira, Data Center or Cloud | `jira` | `jira.issue.created`, `.assigned`, `.transitioned`; `jira_issues{status, category}` |
| A Slack channel's threads | `slack` | `slack.thread.started`, `.replied`, `.checked`, `.unchecked`, `slack.message.posted`; `slack_channel_messages_last_hour{channel}`, `slack_threads_awaiting_reply{channel}`, `slack_threads_unchecked{channel}` |
| A Discord channel's threads | `discord` | the Slack kit's facts in its shapes: `discord.thread.started`, `.replied`, `.checked`, `.unchecked`, `discord.message.posted`; `discord_channel_messages_last_hour{channel}`, `discord_threads_awaiting_reply{channel}`, `discord_threads_unchecked{channel}` |
| PagerDuty | `pagerduty` | `pagerduty.incident.triggered`, `.acknowledged`, `.resolved`; `pagerduty_incidents_open` |
| A Prometheus API (Chronosphere, Mimir) | `promql` | a few PromQL answers as gauges; `schema` lists what the team's `selectors` select (names only, bounded; never every name in the store); prefer Orchestra's own `[[sources]]`, which reads in place with no code |
| Any other JSON API | `http` | a guess at an API no kit knows, for a coding tool to adapt |

- **GitHub's names are one vocabulary**: the Workflows plugin's examples wait on exactly these
  (`team_review`, `delivery_sail`). A script that renames them breaks every workflow that waits on
  GitHub. The subject is `<owner>/<name>#<number>`; a workflow whose item is a ticket maps the
  branch to the ticket key in the script. `branches` (globs, `agent/*`) and `labels` (`SAIL`) narrow
  the pull requests followed; with neither, all. Infer `repo` from `git remote -v`; GitHub
  Enterprise is `base_url = "https://<host>/api/v3"`. Cost: a page of each list a poll, about two
  requests per pull request updated and one or two per open one awaiting its checks; the 5,000 an
  hour is per token, shared with the owner's own `gh`.
  **A team rather than a repository**: `authors` (their logins) and `org` follow their pull
  requests across the organisation's repositories by the search API, one search a poll per author
  (GitHub allows 30 searches a minute per token); issues are not relayed then. Every `github.pr.*`
  carries `tickets`, the keys (`ABC-123`) in the branch name and title (never the title), narrowed
  by `projects`: a linker machine relates the pull request to its ticket from them
  (`item.related {kind: parent}`, the Workflows guide's "whose subject an event is").
- **Jira's edition** (`[settings] edition`; `flavour` is its older name): `cloud` (when not set)
  speaks REST API v3 (`/rest/api/3/search/jql`, paged by `nextPageToken`; Cloud removed `/search`),
  names people by account id and logs in with the account's email and an API token (HTTP Basic:
  `user_file` or `user_env` names the email); `datacenter` speaks REST API v2 (`/rest/api/2/search`,
  paged by `startAt` and `total`), names people by user name (`assignee.name`; there are no account
  ids) and sends a personal access token as `Authorization: Bearer`. `auto` (the template's) is
  `cloud` on `*.atlassian.net` and `datacenter` anywhere else; a Cloud site on its own domain needs
  `cloud` said. The events on the bus are the same for both, so the model never knows which.
  **Telling them apart**: a site on `*.atlassian.net` is Cloud; any other host is almost always Data
  Center (or Server). Once the owner has approved reading it, `python <name>.py edition` reads the
  site's `/rest/api/2/serverInfo` (on both editions) and prints the edition its `deploymentType`
  says (`Cloud`; `Server` or `DataCenter`) and the version; write that into `edition`. **On Data
  Center, ask the owner for** the base URL (`https://jira.<company>`, no `/rest/...`), a personal
  access token (Profile → Personal Access Tokens; Jira 8.14 or later) saved by them in a `chmod 600`
  file outside any repository, named by `token_file` (never the token in `orchestra.toml` or
  `poller.toml`; never ask them to paste it to you), and the board id (the `rapidView=<id>` in the
  board's URL), whose filter and members give `jql` and `users`. The poller only reads (GET). The owner's work
  Jira is Data Center. `jql` is the scope, without `ORDER BY` (the script adds its own); infer the
  project keys from branch names and commit messages (`ABC-123`), and confirm them. `users` (a
  team's user names, from their sprint board) narrows the scope to `assignee in (...) OR reporter
  in (...)`; a separate collector per query (the company's Epics, `issuetype = Epic AND updated >=
  -7d`) keeps each scope's cost visible. `sprint_field` (Data Center: the `customfield_<n>` named
  "Sprint" in `/rest/api/2/field`) puts the issue's sprint's name on each event.
- **Slack**: the template reads with a bot token (`xoxb-`, the bot added to each channel). One
  scope table, the same as Orchestra's INTEGRATING:

  | To | Scopes |
  |---|---|
  | read (the kit: history, threads, reactions, the channel's name) | `channels:history`, `channels:read`; for a private channel also `groups:history`, `groups:read` |
  | post (an Outbox draft, never the kit) | adds `chat:write` |

  No `users:read` (a bot's message says it is one), no `reactions:read` (the parent's reactions
  come with the history). Keep it in `~/.config/orchestra-bridges/slack-token` (`chmod 600`,
  `token_file`). It relays facts, each as who acted: each thread's start, its replies (`first`: the
  first by a person other than the author), the check emoji added (`checked`, as who added it) and
  removed (`unchecked`, as who had checked it, since only they can remove it; when several had,
  as the thread's author with `by: "unknown"`), each with the thread's `permalink`. A channel's
  response policy is the workflow's setting (`orchestra-workflows/examples/slack_queues`), which
  times the first reply from the thread's start; the poller adds no deadline. A user token
  (`xoxp-`) reads the same; the owner's own pings (mentions of them, their DMs, their groups) need
  a user token (`search:read`), since a bot cannot search. An app-configuration token (`xoxe.`)
  reads nothing. `channels` are ids (`C0123`), not names. Slack's history is costly to read
  (`conversations.history` is Tier 3, and far less for an app not in its Marketplace): read each
  channel every 10 minutes or slower (`every`, or a channel's own `cadence`); a channel answered
  429 backs off, doubling to `max_backoff`, while the others go on.
- **Discord**: the template reads with a bot token (`Authorization: Bot <token>`), the bot added to
  the owner's server (guild). What it needs:

  | To | Discord permission or intent |
  |---|---|
  | read (the kit: messages, threads, who reacted, a channel's name, the guild's channels) | **View Channel** and **Read Message History** on each channel (for a private one, the bot or its role added to it) |
  | the Message Content intent | not needed: the kit reads no text (without it Discord sends empty `content`, which it never keeps) |
  | post (an Outbox draft, never the kit) | adds **Send Messages** (and **Send Messages in Threads**) |

  No Server Members intent, no Manage permission, no gateway connection: REST GET only. Keep the
  token in `~/.config/orchestra-bridges/discord-token` (`chmod 600`, `token_file`; never print it
  or ask for it in a chat). `guild` is the server's id; `python <name>.py channels` lists its
  channels (id, type, `#name`: names and ids only, read-only) for the owner to pick `channels` from
  (ids, not names: a text or announcement channel; a forum's posts are not read). It relays the
  Slack kit's facts in its shapes, so a workflow that waits on `slack.thread.*` waits on
  `discord.thread.*` with `who: {"outside": "discord"}`: a thread is a message that is not a
  reply, its replies the messages in the Discord thread started from it and the inline replies to
  it; `checked` is the ✅ (`check_emoji`) on it, published as `emoji: "white_check_mark"` (Slack's
  name), so the workflow's `check_emoji` setting is unchanged; the subject is
  `<channel>/<message id>` and `permalink` `https://discord.com/channels/<guild>/<channel>/<message>`.
  Discord lists who reacted by id, not by time: when two people add the ✅ between polls, `by` is
  one of them. Rate limits: the client waits out a bucket Discord says is empty
  (`X-RateLimit-Remaining: 0` for `X-RateLimit-Reset-After`) and a 429's `Retry-After`; a channel
  still answered 429 backs off, doubling to `max_backoff`, while the others go on. A poll costs per
  channel a page of messages per 100 since the oldest thread followed (`follow`, 2 days), the
  archived threads' page, one guild-wide active-threads read, and a read per thread with a new
  message or a changed ✅ count. **Never**: post, react, create or archive a thread, or read a
  message's text; the client sends GET only, and a reply by the owner is an Outbox draft.
  **What to tell the Supervisor**: "Integrate Discord: guild <id>, these channels (from
  `channels`), the token is in ~/.config/orchestra-bridges/discord-token; read only, never
  post."
- **Values marked `# guess`** in `poller.toml` (the site, the repository, the scope) are the
  template's placeholders: each is the owner's to give, and `integrations` counts what is left.

## The fake first

The scaffold prints the test command with Orchestra's own Python
(`<orchestra's python> -m pytest .orchestra/machines/<name>/test_<name>.py -q`): that Python has
the kits and pytest; `uv run` in the owner's repository uses their environment, which may have
neither. `integrate_system` runs it for you. The test starts the kit's fake, polls it twice with a
restart between, runs `measure`, and checks the events, the series, that nothing was published
twice and that no text or token left.

- **A fake never gets a credential.** Against a loopback URL the runner reads no token and no
  account and sends placeholders (`[settings] fake_token`, `fake_user`, else the kit's), so a real
  token in the environment or a file is sent only to the real host.
- **The cards say it is a fake.** Each actor relayed from a fake is announced with `fake` (and a
  script's `[notes]` add `stand_in` or `disabled`): one line on that machine, the program's own
  words. A demo against fakes is honest about it.
- A failing test, or a system the kits have no template for, is code: `integrate_system` hands it
  to the Coordinator, and a worker adapts the script until its test passes. Never write it yourself.

## Credentials: the human's to create, yours to point at

- **A file first**: `token_file` (and `user_file`), a `chmod 600` file outside any repository
  (`~/.config/orchestra-bridges/<system>-token`), read only by this script. **An environment
  variable second**: `token_env` names it (never holds it), and it must be in the environment
  `orchestra run` starts with; every program `orchestra run` starts inherits that environment, so a
  token there reaches them all.
- `approve_integration` records the references the owner named in `poller.toml`; a path inside a
  repository, or a value that looks like a token, is refused. It reports whether the file is there
  and `chmod 600`, never what is in it.
- Never ask for a token, never accept one pasted into the chat, never write one anywhere. Tell the
  owner where the file goes and that it must be `chmod 600`.
- `[bridges.kits] tokens = { jira = "JIRA_API_TOKEN" }` names a variable per kit for the whole
  project, when a script names none.

## Clearance

`clearance = "needs approval"` (the template's) means the script reads only a fake on this
machine and refuses the real system before any request. The owner's quoted yes, through
`approve_integration`, writes `clearance = "approved"` and the `data_class` (metadata only, by the
kits' rules). You never write the clearance yourself, and a poller started before it does no harm:
it logs `waiting for approval` once, reads nothing, and starts the moment `poller.toml` says
approved.

## What runs it: the entries `approve_integration` writes

Both go to the owner's private overlay through `config_edit`, after the approval and never before
(a `[[collect]]` written early fails every round):

```toml
[[machines]]                      # the poller's process: known to Orchestra only as a process
name = "jira_poll"
command = "python .orchestra/machines/jira_poll/jira_poll.py"
restart = "always"

[[collect]]                       # the instrument that stores its measurement
name = "jira_poll"
command = "python .orchestra/machines/jira_poll/jira_poll.py measure"
every = "5m"
timeout = "60s"
```

- `python` is Orchestra's own Python, and the launcher runs each command from the repository's
  root, so a script in the repository is named by its path there (one in the state directory, by
  its full path).
- `every` is the template's `[settings] every`, 5 minutes: the owner's rule is a live read at most
  every 5 minutes per system. Raise it on a large scope or a shared rate limit; never lower it
  without asking. `per_minute` and `ttl` (a little under `every`) are in the same `[settings]`.
- A promql-only measurement has no `[[machines]]` entry; a store read in place is a `[[sources]]`
  entry instead (`auth.bearer_file` or `auth.bearer_env`).

## `[bridges.kits]`: the project's settings for every collector

```toml
[bridges.kits]
ttl = "4m"                                   # how long a cached answer is served
per_minute = 60                              # the default request rate per host, for every collector
hosts = { "jira.acme.com" = 20 }             # a host's own rate: what its administrators allow
tokens = { jira = "JIRA_API_TOKEN" }         # a kit's variable NAME, when a script names none
ca_file = "~/.config/orchestra-bridges/corporate-ca.pem"   # trusted beside the system's CAs
```

It is checked by `config_edit` like the rest (`a token never goes in orchestra.toml`; an unknown
key is refused); `cache_dir` moves the cache from `<state>/kits/cache`. **Behind a corporate
proxy**, `HTTPS_PROXY` and `NO_PROXY` in the environment of `orchestra run` are honoured by every
collector (a fake on this machine never goes through a proxy); a proxy that re-signs TLS needs its
CA: `ca_file` here for every collector, `[settings] ca_file` for one, or `SSL_CERT_FILE` for
everything in the process (a bundle holding the public CAs too).

## The identity map: `[bridges] people`

```toml
[bridges]
people = "people.toml"   # relative to the repository root, or an absolute path
```

One file for every collector, so one person is one machine across systems: an account it lists on
a system is published as `person.<key>` (`bot.<key>` for a bot) and announced with its name and
team; an account it does not list stays `person.<system>.<account>`. Infer candidates from
`CODEOWNERS` and `git log`, but joining accounts across systems is a claim about people: confirm
with the human before writing it, and always list the owner.

```toml
[people.alice]
name = "Alice Example"
team = "sre"
github = "alice"
jira = "alice"           # Data Center: the user name; Cloud: the account id
slack = "U012AB3CD"      # the member id
```

## RabbitMQ: counted traffic, no code

Not a kit: `orchestra_bridges/rabbitmq/preset.toml` in this package defines series over RabbitMQ's
own per-queue counts, for a project's `[[sources]]` (`preset = ` its installed path: `python -c
"import importlib.resources as r; print(r.files('orchestra_bridges') / 'rabbitmq/preset.toml')"`).
It needs the `rabbitmq_prometheus` plugin's `/metrics/detailed` scraped into a Prometheus-compatible
store (infrastructure: the human's). RabbitMQ never names publishers: fill `[sources.producers]`
from the code, or they are drawn unattributed, which is honest.

## Writes are never the kits'

A collector only reads: no comment, label, transition, reaction or message, ever. An act in the
other system as the owner (a SAIL label, a ticket moved to Done, a Slack post) is an Outbox draft,
which the owner's approval settings govern (the Workflows plugin's `outbox: owner` step). The fakes
accept `PUT`, `POST` and `DELETE` (labels, transitions, reviews, merges) only so a test can change
the world between polls, and so the Outbox's own tests have something to write to.

## Testing a read-only setup: the scripted team

Where the owner's systems may only be read, a poller is tested against a test workspace that the
scripted team fills: `python -m orchestra_bridges.team` plays a working day in a Jira test project
(tickets `[orchestra live test] …`, labelled `orchestra-live-team`) and Discord test channels, and
`--close` closes its tickets afterwards (it never deletes). It is test tooling, never a machine:
never declare it, schedule it or write an entry for it; the pollers relay its acts as the accounts
it used. Running it against a real workspace is the owner's decision (it writes there); `--dry-run`
shows the plan. README, "The scripted team".

## What goes wrong

- **`HTTP 401`** in the script's log (`<state>/logs/machines/<name>.log`): a bad or expired token,
  or for Jira Cloud a token without its account's email. **`403`**: a missing scope, or a rate
  limit. **`404`**: a wrong repository, project or site, or one the token cannot see. **`410`**
  from Jira: a Data Center script against Cloud (`edition`); a `404` on `/rest/api/3/…`: a Cloud
  script against Data Center.
- **`waiting for approval`** forever: the clearance was never written; ask the owner.
- **`reading … is not cleared`** and exit 2: `--once` or `measure` against the real host before the
  approval (the `[[collect]]` written too early).
- **A token named but not set**: `approve_integration` says so; the owner sets it and restarts
  `orchestra run`, or (better) puts it in a file.
- **No events but the series move**: nothing changed within the scope, or the scope is wrong
  (`jql`, `channels`, `branches`); the Jira and GitHub kits' first run starts now (`since`: a duration back reads a catch-up window of the items not yet done), and the others read back only `lookback`.
- **Old events after a restart**: a resumed poller reads on from its cursor, so what was done
  while it was stopped is relayed once, when it starts; each such event (older than the run's
  start by more than `every`) carries `replayed: true` in its data, and a workflow's start
  refuses it with `"replayed": {"not": [true]}`.
- **The same thing twice**: never from a re-read (an event's id is the item's id and its change's
  stamp); two collectors over one scope, or a script edited to stamp by time, would.
- **A dependency missing**: the kits not installed in Orchestra's Python (`python -c "import
  orchestra_bridges.kits"`); pytest is a dependency of the plugin, so the test runs where the kits do.

## The human's decisions

Ask; do not decide:
- whether a system may be read at all, and the data class (their quoted yes);
- creating any token, app or `gh` login, the scopes granted, and where it is kept (a file's path or
  a variable's name; they put the token there);
- the scope: which repositories, projects, channels or services;
- whether the repository must stay clean (the state directory instead);
- who is who in the identity map;
- polling harder than every 5 minutes, or spending a rate limit others share;
- every write to the system, each through the Outbox;
- restarting `orchestra run` so an environment variable applies.
