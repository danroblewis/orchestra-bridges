# Bridges: an operator's guide

For the AI operator (the Supervisor) that configures this plugin in a user's project. Decision
knowledge, not a procedure: what each setting does, how to choose it, what goes wrong, and which
choices are the human's. Every key named here is read by `orchestra_bridges/config.py`; nothing
else is.

## What the plugin is

The bridges are relays. Each one polls another system (GitHub issues, GitHub pull requests, Jira,
Slack) with the owner's credentials and publishes what people did there on the project's bus, as
the people who did it, metadata only (never titles, bodies or messages). They only read: none
comments, labels, reacts or transitions. A bridge is never a machine; the people and bots it
relays are, and they appear on the Machines page only once they act.

- A bridge runs **only where its section exists**: `[bridges.github]`, `[bridges.github_prs]`,
  `[bridges.jira]` (and `[bridges.jira_<name>]` for a second Jira site), `[bridges.slack]`. For
  each, `orchestra run` keeps the process `bridges.<section>` running and scrapes its `/metrics`
  every 30 s as the instrument `bridges.<section>`. No section, nothing runs.
- The sections are read from the merged configuration: `orchestra.toml` with the private overlay
  over it. Orchestra itself ignores `[bridges]`; only this plugin reads it, and only if the
  `orchestra-bridges` package is installed in Orchestra's environment (installing it is the human's).
- RabbitMQ is not a bridge: it is a preset for Orchestra's own `[[sources]]` (last section).

## How a change takes effect

- Change settings only through `project.config_edit`. It runs the plugin's check
  (`Plugin.check`, `orchestra_bridges.check.check`) by itself; pass no `check=`. The check runs
  every configured section through the bridge's own reader, and also reports a key no bridge reads
  (a misspelt key is otherwise silently ignored), a `[bridges.<x>]` that is no bridge, and two
  bridges on one `/metrics` port. config_edit refuses a change that makes it say something new,
  with `bridges:` before what it said.
- Set a new section's required keys in **one** change (`set_many`): `[bridges.jira]` without
  `api` and one of `projects`/`jql`, or `[bridges.github]` or `[bridges.github_prs]` without `repos`, is refused.
- Adding a section starts its bridge at once; removing it stops it; changing a setting in it (or
  `[bridges] people`) restarts it with the new value within seconds. A changed identity map file
  (`people.toml` itself) is read only when the bridge next starts.
- What the check cannot see (tokens, network, a repository the token cannot read) shows only when
  the bridge runs. One that cannot start (no token, port taken) exits with code 2 and is retried
  with backoff; after 5 failures in a row, each within 60 s of starting, the launcher gives up until
  its section changes (fixing the setting is enough) or `orchestra run` restarts. A token fixed
  outside the configuration needs a change to the section or that restart. Its log:
  `<state>/logs/machines/bridges.<section>.log`.

## Where each setting goes

- **Never a credential, in either file.** Tokens come from `gh`, the environment or a token file
  (next section). The Jira, Slack and GitHub PR readers refuse a `token` key; the check refuses it
  everywhere.
- **The repository's `orchestra.toml`** (committed, shared with the team): what the team agrees to
  watch: `[bridges.github]` and `[bridges.github_prs]` repositories, branch globs, labels.
- **The private overlay** (never committed): what is the owner's own. `[bridges.slack]` belongs
  there: it reads the owner's pings with the owner's token, and in the repository it would start a
  Slack bridge for every teammate who runs `orchestra run`. So do Jira `email`, `token_file`, and
  a Jira section whose levels are about "mine" (the token's owner). A table merges key by key, so
  the overlay can also change one key of a section the repository has.

## Credentials: the human's to create, yours to point at

| Bridge | What it uses, in order |
|---|---|
| github, github_prs | `GH_TOKEN` or `GITHUB_TOKEN` (for an Enterprise `api`: `GH_ENTERPRISE_TOKEN` or `GITHUB_ENTERPRISE_TOKEN`), else `gh auth token --hostname <api's host>`: the owner's `gh` login is enough |
| jira | `JIRA_API_TOKEN`, else the file `JIRA_API_TOKEN_FILE` names, else `token_file`, else `~/.config/orchestra-bridges/jira-token`. `jira_<name>` reads `JIRA_<NAME>_API_TOKEN`, `.../jira_<name>-token` |
| slack | `SLACK_USER_TOKEN`, else `SLACK_USER_TOKEN_FILE`, else `token_file`, else `~/.config/orchestra-bridges/slack-token` |

- A token file must be **outside the repository** and **`chmod 600`**, or the bridge refuses it.
- Prefer files to environment variables: `orchestra run` passes its environment to every process
  it starts, so a token in it reaches every machine.
- Jira Data Center: a personal access token (read access is enough), sent as Bearer. Jira Cloud:
  an API token plus the account's email (`email`, or `JIRA_EMAIL` / `JIRA_<NAME>_EMAIL`).
- Slack: a **user** token (`xoxp-`), from a Slack app the owner creates, with user scopes
  `search:read` (always) and `reactions:read` (for the default checkmark rule). A bot token cannot
  search. A workspace may require an admin to approve the app.
- Never ask the human to paste a token to you, and never write one anywhere. Tell them where the
  file goes and that it must be `chmod 600`.

## Settings every bridge has

```toml
[bridges.github_prs]
repos = ["acme/service"]
every = "60s"          # pause after each round (at least 5s); the round's own time adds to it
lookback = "1h"        # first start only: how far back to read
max_catchup = "48h"    # after a gap, how far back it reads at most
overlap = "10m"        # each poll re-reads this much before the last
host = "127.0.0.1"     # where /metrics listens
port = 9466            # where /metrics is served; 0 = any free port and no scrape
```

- Durations: a number is seconds; a string takes `ms`, `s`, `m`, `h`, `d` or `w`.
- `every` trades freshness for requests. Waits are measured in minutes, so 60 s is right almost
  everywhere; raise it for many repositories or pull requests (rate limits, per bridge below). A
  gap (a laptop asleep, a bridge stopped) is noticed only when a poll comes more than 3 × `every`
  after the last good one.
- `lookback` matters only the very first time (nothing of the bridge's on the bus or in the store).
  A long one publishes history as if new; `1h` is a safe start.
- `max_catchup` bounds the catch-up after a gap; what lies beyond is lost and counted as an
  unrecovered gap. Larger costs a burst of requests after a long sleep; 48 h covers a weekend
  only partly; `72h` if the laptop sleeps over weekends and the rate limits allow.
- `overlap` covers late arrivals (search indexing, Jira's clock, a comment stamped before a close
  already read). Re-reading never duplicates (ids are the system's). GitHub issue counts wait this
  long before counting an event. Keep `10m`; larger than the clock skew between Jira and here.
- `host`: keep `127.0.0.1`. A wildcard (`0.0.0.0`) exposes the counts to the network; the human
  decides that.
- `port`: defaults 9464 (github), 9465 (slack), 9466 (github_prs), 9467 (jira); a `jira_<name>`
  section must name its own. Change one only on a clash with something else on the machine (the log
  says the port is taken). `port = 0` leaves the counts unstored and a restart without its last
  poll, so avoid it outside tests. A project's own `[[scrape]]` named `bridges.<section>` replaces
  the plugin's and must keep exactly that name: a restarted bridge finds its counts by it.

## GitHub issues: `[bridges.github]`

Publishes issues opened, commented, labelled, closed, reopened (pull requests left out), and
measures first response: issues waiting for anyone but their author.

```toml
[bridges.github]
repos = ["acme/service", "acme/web"]   # owner/name
bots_answer = false                    # true: a bot's comment or close counts as a first response
api = "https://api.github.com"         # GitHub Enterprise: https://<host>/api/v3
```

- **`repos` is required.** There is no default: a section without it is refused
  (`[bridges.github] repos is required`), so set it in the same change that adds the section.
- Infer `repos` from `git remote -v` (`git@github.com:acme/service.git` → `acme/service`). A remote
  on another host is GitHub Enterprise: `api = "https://<host>/api/v3"`, and the owner needs
  `gh auth login --hostname <host>`.
- `bots_answer`: false unless a bot's reply is a real answer for this team (a support bot that
  triages and replies). Labels never count as an answer.
- Cost: at least three paged lists per repository per round, 180 requests an hour per repository
  at 60 s. The 5,000 an hour is per token, shared with the PR bridge and the owner's own `gh` use.

## GitHub pull requests: `[bridges.github_prs]`

Follows pull requests to merge or close: opened, pushed, review requested and submitted, line
and conversation comments, labels, checks, merged, closed, reopened. Measures what waits on whom
(awaiting review, awaiting changes, approved, checks failing, awaiting a label's result).

```toml
[bridges.github_prs]
repos = ["acme/service"]            # required
branches = ["agent/*", "fix/*"]     # follow pull requests whose head branch matches a glob,
labels = ["SAIL"]                   # or that carry one of these labels; neither: all of them
checks = true                       # read the head's check runs and statuses each round

[bridges.github_prs.label_results]
SAIL = ["SAIL-passed", "SAIL-failed"]   # carrying SAIL and neither result: awaiting a result
```

- Narrow with `branches` to the branches machines push (the agents' or a bot's prefix: look at
  `git branch -r` and the agents' configuration) when the owner cares about those only; leave both
  empty to follow every pull request. A followed one stays followed until merged or closed.
- `label_results`: for a pipeline driven by labels (a label submits, others report). Read the
  repository's `.github/workflows` for `on: pull_request: types: [labeled]` and the labels they
  test and set. Applying the submitting label stays the owner's act; the bridge never labels.
- Cost: about 2 requests a repository plus 3 per open followed pull request each round (1 without
  checks), plus one per new review and per new head. Ten open pull requests at 60 s use about
  1,900 of the 5,000 an hour. On a busy repository raise `every`, narrow `branches`, or set
  `checks = false` (then `github_prs_checks_failing` is not measured). On a primary limit run out it
  waits for the reset when that is within 15 minutes, else the poll fails and the next tries.
- Only the first 100 check runs on a head are read. A machine acting through the owner's GitHub
  account appears as the owner.

## Jira: `[bridges.jira]`, `[bridges.jira_<name>]`

Publishes issues created, assigned, transitioned, updated (sprint, priority, labels, links, due
date) and commented. Measures issues by status category, the owner's not-started issues and
comments on them the owner has not answered.

```toml
[bridges.jira]
api = "https://jira.acme.com"   # required; https (http only to a loopback fake)
deployment = "datacenter"       # or "cloud"; default: cloud on *.atlassian.net, else datacenter
projects = ["ABC", "OPS"]       # project keys, or instead:
# jql = "project = ABC AND component = api"   # exactly one of projects and jql
done_every = "1h"               # how often done issues are counted; 0 = never
bots_ask = false                # true: a bot's comment waits for the owner's answer too
# sprint_field = "customfield_10100"          # only if the sprint field found is wrong
# token_file = "~/.config/orchestra-bridges/jira-token"   # a path, never the token
```

- **The owner's Jira is Data Center**, on the company's own host: the default for any `api` not on
  `*.atlassian.net`. Set `deployment = "cloud"` only for a Cloud site on a custom domain.
- Infer `api` and `projects` from the codebase: issue keys in branch names and commit messages
  (`ABC-123` → `ABC`), `/browse/ABC-1` links in READMEs, PR templates or CI config (their host is
  `api`). Keys: an upper case letter, then upper case letters, digits or `_`.
- `jql` is for a narrower scope (a component, a team's label). It is wrapped in parentheses and
  given its own clauses, so it must not contain `ORDER BY`. Prefer `projects` when it fits.
- `done_every` reads every done issue's key: hourly is right; `0` on a huge project.
- `sprint_field` is found from the site's fields (the Jira Software sprint type). Set it only when
  the site has several and the wrong one is picked.
- `email` is Cloud only (the account the API token belongs to); it is personal, so the overlay.
- **A second site** (a FedRAMP instance, say) is its own section with its own `api`, scope, token
  and a required `port`. Name it `jira_` plus lower case letters or digits:

```toml
[bridges.jira_fedramp]
api = "https://jira.fedramp.acme.com"
projects = ["CUST"]
port = 9468
```

- Cost: about four searches a round, a page per 50 issues, plus one request per changed issue
  whose changelog or comments the search cut short. A 429 or 503 is waited out (at most 15 min).
  A Data Center admin may rate-limit tokens; if the log shows 429s, raise `every`.
- The owner is the token's account. On start the log says if that account is not in the identity
  map and names the value to list (`jira = "<username>"`, or the Cloud account id).

## Slack pings: `[bridges.slack]` (the overlay)

Measures the owner's promise to answer pings: mentions of the owner, DMs, mentions of the owner's
groups; what is waiting and how long answers took. It reads every message that mentions the owner
(text in memory only): enabling it is the human's decision.

```toml
[bridges.slack]
answered = ["reaction", "reply"]   # what answers a ping: a checkmark, the owner's reply, or both
reactions = ["white_check_mark", "heavy_check_mark"]   # names without colons
groups = ["S0123ABCD"]             # user group ids whose mentions are pings too
dms = true                         # whether a direct message is a ping
expire = "168h"                    # an unanswered ping stops waiting after this
```

- `answered` defines the promise; ask the human how they answer. The default is `["reaction"]`
  only: then a ping answered only by replying waits until `expire` and counts as expired. Someone
  who replies as often as they react wants both. It can never be empty.
- A checkmark's time is unknown to Slack's API, so its answer is stamped when seen (up to a poll
  late); a reply has its own time. `reactions.list` reads the owner's 500 latest reactions at most.
- `groups`: user group ids (`S…`, from the group's page in Slack), not handles. Only the human
  knows which groups count as pinging them.
- `expire`: a ping not answered by then is dropped from the waiting stock and counted as expired.
  7 days is right for most; shorter if pings go stale sooner.
- `queries` replaces all the default searches (`<@{me}>`, one per group, `to:<@{me}>` when `dms`;
  `{me}` is filled with the owner's member id). Set it only if the bridge's log shows real pings
  missed; a replacement must keep the group and DM searches or those pings are never found.
- `api` must be https on slack.com (or a loopback fake); leave it.
- Cost: per poll, one `search.messages` per search per page of 100, one more search with the reply
  rule, and up to five `reactions.list` while a ping waits under the checkmark rule. Well under
  Slack's limits at 60 s; a `Retry-After` longer than `every` fails that poll.
- Waits are wall-clock, nights and weekends included.

## The identity map: `[bridges] people`

```toml
[bridges]
people = "people.toml"   # relative to the repository root, or an absolute path
```

One file for every bridge, so one person is one machine across systems. Without it, people are
`person.<login>` (GitHub), `person.jira.<user>` and `person.slack.<handle>`. A missing file means
nobody is listed (silently); a file that is not valid TOML stops every bridge at start. `~` is not
expanded here: use an absolute path for a file outside the repository (the owner's private map;
set `bridges.people` in the overlay then).

```toml
[people.daniel]
name = "Daniel Lewis"
team = "sre"
github = "danroblewis"
jira = "dlewis"          # Data Center: username or user key (JIRAUSER10100); Cloud: account id
slack = "U012AB3CD"      # member id or handle

[people.automation]
name = "Jira automation"
bot = true               # a program, not a person; its comments never wait for an answer
jira = ["automation", "jira-bot"]
```

- Always list the owner: their answers and their Jira levels then carry their name and team.
- Infer candidates from `CODEOWNERS` (GitHub logins and teams) and `git log` names, but joining
  accounts across systems is a claim about people: confirm with the human before writing it.

## RabbitMQ: counted traffic (Orchestra's `[[sources]]`)

Not a bridge and not a `[bridges]` section: the file `orchestra_bridges/rabbitmq/preset.toml` in
this package defines series over RabbitMQ's own per-queue counts, for a project's `[[sources]]`.

```toml
[[sources]]
name = "rabbit"
url = "http://127.0.0.1:8441"   # the Prometheus-compatible store that scrapes RabbitMQ
preset = "/path/to/orchestra_bridges/rabbitmq/preset.toml"   # where the package is installed
settle = "30s"                  # VictoriaMetrics hides the newest 30 s from range queries

[sources.producers]             # exchange = the machine that publishes into it
orders = "orders-api"
```

- Needs the `rabbitmq_prometheus` plugin's `/metrics/detailed` scraped into that store with the
  families the preset's header lists. Setting that up is infrastructure: the human's.
- A queue is read as its consumer's by name, `<consumer>.<anything>`; each queue must be bound to
  one exchange. Other naming needs a copy of the preset with a changed regex.
- RabbitMQ never names publishers. Fill `[sources.producers]` from the code (`basic_publish` and
  the exchange each service names); unnamed producers are drawn unattributed, which is honest.

## Checking that it works

1. config_edit accepted the change with the bridges' check: the settings read.
2. The process `bridges.<section>` is running, and its log shows `serving http://…/metrics`, then
   `resumed: …`, then one line a round (`N new events; …`). `poll failed, tried again next round:
   GET <path>: HTTP 401` is a bad or expired token; `403` a missing scope or a rate limit; `404` a
   wrong repository, or one the token cannot see.
3. The Instruments page lists `bridges.<section>` with its series (`github_issues_unanswered`,
   `github_prs_open`, `jira_issues`, `slack_pings_unanswered`, and the gap counts).
4. People appear on the Machines page only when they act: a quiet repository shows none for a while.

## The human's decisions

Ask; do not decide:
- creating any token, Slack app or `gh` login, and the scopes granted;
- which systems to read at all (Slack reads the owner's messages; Jira and GitHub read colleagues'
  activity) and which repositories, projects or groups;
- what counts as an answer (`answered`, `bots_answer`, `bots_ask`) and when a ping expires;
- polling harder than the defaults, when it spends a shared rate limit;
- exposing `/metrics` beyond loopback;
- who is who in the identity map;
- restarting `orchestra run` so a changed setting applies.
