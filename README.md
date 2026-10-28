# orchestra-bridges

Bridges for [Orchestra](https://github.com/danroblewis/orchestra): programs that poll systems
which are buses in all but name and publish what people did there as events on a project's
Orchestra bus. Each event's source is the person (or bot) who acted and its subject is the item
they acted on, so people and bots appear on Orchestra's Machines page beside the programs, and
Orchestra infers who hands work to whom from the subjects. The events hold metadata only, never
text.

How it works, with diagrams, the contracts and how to add a bridge: [ARCHITECTURE.md](ARCHITECTURE.md).

The first bridge reads **GitHub issues**; the second follows **GitHub pull requests** to
resolution; the third follows **Jira** issues (Data Center, or Cloud). Slack is on a branch;
Confluence and Google Drive come later (Orchestra's `docs/proposals/event-sources.md` §4).

## The GitHub issues bridge

Every `every` seconds it reads a repository's issues, issue comments and issue events through the
REST API, and publishes:

| Event | Source | Data (no text) |
|---|---|---|
| `github.issue.opened` | the author | repository, issue number, login, association |
| `github.issue.commented` | the commenter | the same, and the comment's id |
| `github.issue.labelled` | who added the label | the same, and the label's name |
| `github.issue.closed` | who closed it | the same, and the close reason |
| `github.issue.reopened` | who reopened it | the same |

The subject is the issue (`microsoft/vscode#1234`). The time and the id come from GitHub, so a
poll that overlaps the last one never publishes anything twice. Pull requests are left out. The
first time it sees someone, it announces them (`orchestra.machine.started`: kind `person`, or
`program` for a bot; package `bridges`).

It also writes these series, labelled `machine="bridges.github"` and `repo`:

- `github_issues_unanswered`: issues opened while the bridge watched that nobody but the author
  has yet commented on or closed;
- `github_issues_unanswered_oldest_seconds`: how long the oldest of them has waited;
- `github_issues_opened_total`, `github_issues_answered_total{within="15m|1h|1d|over"}`,
  `github_issues_closed_unanswered_total`: what moves the level, so that
  unanswered = opened − answered − closed unanswered;
- `github_issue_first_response_seconds_total`: the first-response waits added up. The mean wait
  is its rate divided by the answered rate.

A label is not a response, since triage bots label everything. A bot's comment or close counts as
a response only with `bots_answer = true`.

### Configure

In the project's `orchestra.toml`:

```toml
[bridges]
people = "people.toml"            # optional: the identity map, relative to the repository

[bridges.github]
repos = ["microsoft/vscode"]      # the default
every = "60s"
lookback = "1h"                   # on the very first start, how far back to read
max_catchup = "48h"               # after a gap, how far back it reads at most
overlap = "10m"                   # each poll reads again this far before the last one
bots_answer = false
```

With this package installed in the environment that runs Orchestra (`uv add` it, or
`uv pip install -e ../orchestra-bridges`), `orchestra run` starts the bridge as the process
`bridges.github`, logs it to `<state>/logs/machines/bridges.github.log` and restarts it if it
exits. The Machines page lists it before its first event. Without a `[bridges.github]` section,
nothing runs.

To try one poll by hand: `ORCHESTRA_REPO=/path/to/project uv run python -m orchestra_bridges github --once`.

**Credentials.** For github.com it uses `GH_TOKEN` (or `GITHUB_TOKEN`) when one is set; for a
GitHub Enterprise `api`, `GH_ENTERPRISE_TOKEN` (or `GITHUB_ENTERPRISE_TOKEN`). Otherwise it runs
`gh auth token --hostname <the api's host>` once at start, so your `gh` login for that host is
enough, and an Enterprise API never gets your github.com token. The token stays in memory. It is
never written, logged or published, and it is sent only to the configured API's scheme and host: a
redirect or a pagination link to anywhere else is not followed.

**People.** Someone not in the identity map is `person.<login>`, and a bot is `bot.<login>`. The
map gives a person a name and a team, and joins their accounts across systems:

```toml
[people.daniel]
name = "Daniel Lewis"
team = "sre"
github = "danroblewis"
```

That person is then `person.daniel`, announced with their name and team.

### When the laptop sleeps

The bridge keeps no state of its own. When it starts, it reads its own events back from the bus,
rebuilds its counts from them, and reads GitHub again from where it left off. If a poll comes more
than three rounds after the last one (because the machine slept or the bridge was stopped), it
publishes `bridge.gap` {repository, from, to, recovered} and catches up: the events it missed are
published with their GitHub times, and the series are filled in at those times too. A gap longer
than `max_catchup` is read only back to that limit, and `recovered` is false.

### Limits, plainly

- Issues opened before the bridge first watched are not counted as waiting.
- A reopened issue does not wait again.
- How far back a busy repository's issue events can be paged is up to GitHub. `max_catchup`
  bounds what the bridge asks for.
- A series point is never written before one already written. So a change that GitHub reports a
  poll late is stamped up to one poll late. Only a gap, when nothing was written, gets its true
  times.
- It remembers the ids of everything it has published for as long as it runs. That is a few
  thousand a day for a busy repository.
- Busy repositories have hundreds of people, and each one becomes a machine.

## The GitHub pull-request bridge

It follows pull requests until they are merged or closed, and publishes what happens on them, so
that whatever wrote a change (a worker, through the task system) can be told when a review, a
review comment or a pipeline's label arrives. It only reads: it never pushes, labels, comments or
merges. Submitting to a pipeline (adding `SAIL`, say) stays the owner's own act.

Every `every` seconds (after the last round) it reads each configured repository's open pull
requests, the timeline of each it follows, and the head's checks, and publishes:

| Event | Source | Data besides the common fields (no text) |
|---|---|---|
| `github.pr.opened` | the author | `association`, `draft` |
| `github.pr.pushed` | who pushed (see below) | `previous` (the head before), `forced`, `pusher_from` |
| `github.pr.review_requested`, `.review_request_removed` | who asked | `reviewer` (login) or `team` (slug) |
| `github.pr.review_submitted` | the reviewer | `review` (id), `state` (`approved`, `changes_requested`, `commented`, `dismissed`), `comments` (how many line comments), `association` |
| `github.pr.review_commented` | the reviewer | `review`, `comment` (ids), `path`, `line`, `start_line`, `side`, `subject_type`, `in_reply_to` |
| `github.pr.commented` | the commenter | `comment` (id), `association`: a comment on the conversation, not on a line |
| `github.pr.labelled`, `.unlabelled` | who changed the label | `label` (name) |
| `github.pr.check_completed` | the app (`bot.github-actions`), or who set the status | `check` (name or context), `conclusion`, `via` (`check_run` or `status`), `app` |
| `github.pr.merged` | who merged | `merge_commit` |
| `github.pr.closed` | who closed it unmerged | `merged: false` |
| `github.pr.reopened` | who reopened it | |

Every event's data also holds `repository`, `pull` (the number), `login` (who acted), `branch` (the
head branch), `base`, and `head`: the commit the event is about (the commit reviewed, the new head,
the commit checked), else the pull request's head when it was read. The subject is the pull
request (`acme/service#12`); the id and time are GitHub's (`github:<repo>:pr:<n>:review:<id>`, …),
so re-reading never publishes twice. A merge's own close is not published as `closed`.

**Which pull requests.** Those whose branch matches one of `branches` (globs) or that carry one of
`labels`; with neither, all of them. Once followed, a pull request is followed until it is merged
or closed. On a first start, an open one is taken up with its (perhaps old) opening; a closed one
only if it was opened inside the window read.

**Who pushed.** GitHub records the actor of a force push, but not of an ordinary one. The bridge
then names the head commit's GitHub committer (or author), and failing that the pull request's
author; `pusher_from` says which (`force_push`, `committer`, `author`, `pull_author`). Several
pushes between two polls are one `pushed`. A push is stamped with the commit's date when that falls
in the window read, else when it was seen.

**What waits on whom.** It serves these levels on `/metrics`, labelled `repo`, read from GitHub's
state each round over the open pull requests it follows; the plugin declares the scrape that
stores them (the instrument `bridges.github_prs`):

- `github_prs_open`;
- `github_prs_awaiting_review`: not drafts, with a review asked and not given;
- `github_prs_awaiting_changes`: a reviewer's latest verdict is "changes requested" on the commit
  that is still the head;
- `github_prs_approved`: approved, and nobody's latest verdict is "changes requested";
- `github_prs_awaiting_label_result`: carrying a label of `label_results` and none of its results;
- `github_prs_checks_failing`: the head's latest run of some check or status failed;
- `github_prs_review_request_oldest_seconds`: how long the oldest open review request has waited;
- `github_prs_bridge_last_poll_timestamp_seconds`: when its last good poll began (for resume).

Flows (opened, reviewed, merged) are its events, which Orchestra counts by type itself.

### Configure

```toml
[bridges.github_prs]
repos = ["acme/service"]          # required
branches = ["agent/*"]            # follow these branches,
labels = ["SAIL"]                 # or pull requests carrying these labels; with neither, all
every = "60s"
lookback = "1h"
max_catchup = "48h"
overlap = "10m"
checks = true                     # read the head's check runs and statuses
host = "127.0.0.1"                # /metrics: local only unless you change it
port = 9466                       # 0: any free port, and no scrape is declared

[bridges.github_prs.label_results]
SAIL = ["SAIL-passed", "SAIL-failed"]   # carrying SAIL and neither of these: awaiting a result
```

`orchestra run` starts it as `bridges.github_prs` and scrapes its `/metrics` every 30 s. To poll
once by hand: `ORCHESTRA_REPO=/path/to/project uv run python -m orchestra_bridges github_prs --once`.
Credentials are the issues bridge's: `GH_TOKEN`, or your `gh` login, never a token in
`orchestra.toml` (a `token` key is refused).

**Cost.** A round makes about 2 requests per repository and 3 per followed pull request (its
timeline and its head's checks and statuses), plus one per new review and one per new head. Ten
open pull requests polled every minute use about 1,900 of the 5,000 requests an hour GitHub
allows. Raise `every`, narrow `branches`, or set `checks = false` on busy repositories.

### When the laptop sleeps

As for issues: it keeps nothing of its own. On start it reads its own events back from the bus,
which say what it published, which pull requests it follows and which heads it has seen, and the
store gives its last good poll. A poll more than three rounds after the last good one publishes
`bridge.gap` {repository, from, to, recovered} and catches up: what was missed is published once,
with GitHub's times, back to `max_catchup` at most.

### Limits, plainly

- A review only commenting gives no verdict; a dismissed one withdraws the reviewer's.
- Reviews still pending (drafts) are invisible to the bridge, as they are to everyone but their
  author.
- Only the first 100 check runs on a head are read.
- On one's own pull request GitHub refuses "request changes", so the sandbox test (below) can only
  comment.
- A worker that acts through the owner's GitHub account appears as the owner.

## The Jira bridge

It follows the issues of some Jira projects (or those a JQL query matches) on one site, polls once a
minute, and publishes what people did to them: created, assigned, moved through the workflow,
re-prioritised, re-labelled, put in a sprint, linked, commented on. It only reads. Commenting,
transitioning or creating issues as the owner is the Outbox's work, which the Supervisor configures
separately; nothing here writes to Jira.

**Data Center first.** The owner's Jira is Data Center: the bridge uses its REST API v2
(`/rest/api/2/search` with `expand=changelog`, paged by `startAt`) and a personal access token.
Jira Cloud works too (`/rest/api/3/search/jql`, paged by `nextPageToken`, an API token with the
account's email), because the difference is only the search's paging and the way to sign in.

| Event | Source | Data besides the common fields (no text) |
|---|---|---|
| `jira.issue.created` | the creator | `status`, `status_id`, `category`: the status it started in |
| `jira.issue.assigned` | who assigned it (the creator, for an assignee set at creation) | `assignee`, `previous` (accounts), `assignee_machine` (the new assignee as a machine) |
| `jira.issue.transitioned` | who moved it (to another status: never a status to itself) | `from`, `to` (status names), `from_id`, `to_id`, `from_category`, `to_category` (`new`, `indeterminate`, `done`) |
| `jira.issue.updated` | who changed it | `fields` (names) and `changes`: `priority` and `duedate` from and to (ids, dates), `labels` added and removed (names), `sprint` from and to (ids), `link` from and to (issue keys) |
| `jira.comment.added` | the commenter | `comment` (id), `comments` (its place among the issue's comments: 1 for the first), `author`, `restricted` (visible to a role or group only) |

Every event's data also holds `site` (the host), `project` (key), `issue` (Jira's id), `key`,
`issuetype` (name), `login` (who acted) and the issue as the bridge read it: `status_category`,
`priority` (name) and `priority_id`, `labels`, `sprints` (ids of sprints not closed) and `links`
(keys of linked issues). The subject is the issue's key (`ABC-12`). The time and id are Jira's
(`jira:<site>:issue:<issue id>:history:<changelog id>:status`, …), so re-reading never publishes
twice; the id uses the issue's id, not its key, so an issue moved to another project keeps its ids.
Summaries and descriptions are never even asked for; comment bodies, and the before and after of a
text field's change, pass through memory and are dropped as they are read. A change Jira made itself
(no author: a workflow post function, an import) is published as `bridges.jira`.

**What waits** is served on `/metrics` and stored by the instrument `bridges.jira` the plugin
declares (every 30 s), read from Jira each round:

- `jira_issues{jira_project, category}`: issues by status category. `done` is counted every
  `done_every` (it reads every done issue's key), the rest every round;
- `jira_issues_mine_not_started{jira_project}`: assigned to the owner (whoever the token belongs to)
  and in a `new` status;
- `jira_comments_unanswered{jira_project}`: the owner's open issues where someone else commented
  since the owner last did. A bot's comment does not wait unless `bots_ask`;
- `jira_comment_unanswered_oldest_seconds{jira_project}`: how long the oldest of those has waited;
- `jira_bridge_last_poll_timestamp_seconds`: when the last good poll began (for resume).

The label is `jira_project`, because `project` is Orchestra's own. Flows (created, transitioned,
commented) are the events, which Orchestra counts by type itself.

### Configure

Every key, for the owner or the Supervisor to set, in the project's `orchestra.toml`:

```toml
[bridges]
people = "people.toml"            # the identity map (below), relative to the repository; shared by every bridge

[bridges.jira]
api = "https://jira.acme.com"     # required: the Data Center base URL (or https://<site>.atlassian.net)
deployment = "datacenter"         # or "cloud"; default: cloud on *.atlassian.net, else datacenter
projects = ["ABC", "OPS"]         # follow these projects' issues,
# jql = "project = ABC AND component = api"   # or the issues this matches (exactly one of the two; no ORDER BY)
every = "60s"                     # pause between polls (at least 5 s)
lookback = "1h"                   # on the very first start, how far back to read
max_catchup = "48h"               # after a gap, how far back it reads at most
overlap = "10m"                   # each poll reads again this far before the last one
done_every = "1h"                 # how often done issues are counted; 0: never
bots_ask = false                  # true: a bot's comment waits for the owner's answer too
sprint_field = "customfield_10100"   # optional: found from /rest/api/2/field when not set
token_file = "~/.config/orchestra-bridges/jira-token"   # optional: where the token is (never the token)
email = "me@acme.com"             # Cloud only: the account the API token belongs to (or JIRA_EMAIL)
host = "127.0.0.1"                # where /metrics listens: local only unless you change it
port = 9467                       # 0: any free port, and no instrument is declared
```

**A second site** (the FedRAMP instance, say) is a second section named `jira_<name>`, with its own
`api`, projects, token and a `port` of its own (required):

```toml
[bridges.jira_fedramp]
api = "https://jira.fedramp.acme.com"
projects = ["CUST"]
port = 9468
```

It runs as its own machine, process and instrument, `bridges.jira_fedramp`, and its token is
`JIRA_FEDRAMP_API_TOKEN` or `~/.config/orchestra-bridges/jira_fedramp-token`, so neither site ever
receives the other's token.

**The token** is never in `orchestra.toml` (a `token` key is refused). The bridge reads it once at
start, in this order: the variable `JIRA_API_TOKEN` (`JIRA_<NAME>_API_TOKEN` for `jira_<name>`);
the file `JIRA_API_TOKEN_FILE` names; the file `token_file` names; `~/.config/orchestra-bridges/jira-token`.
A file must be outside the repository and `chmod 600`. On Data Center it is a personal access token
(your profile, Personal Access Tokens; read access is all it needs) sent as `Authorization: Bearer`.
On Cloud it is an API token, sent with `JIRA_EMAIL` (or `email`) as HTTP Basic. The token stays in
memory and goes only to `api`'s scheme and host: a redirect elsewhere is refused. `api` must be
https, except a loopback address for a fake.

**People.** The identity map names a Jira account under the key `jira`, the same map the other
bridges use, so one person is one machine on every system:

```toml
[people.daniel]
name = "Daniel Lewis"
team = "sre"
github = "danroblewis"
jira = "dlewis"                   # Data Center: the username, or the user key (JIRAUSER10100); Cloud: the account id

[people.automation]
name = "Jira automation"
bot = true                        # announced as a program, and its comments never wait for an answer
jira = ["automation", "jira-bot"] # several accounts (two sites, say) may be listed
```

A listed account is `person.<key>` (`bot.<key>`). Anyone else is `person.jira.<username>` (Cloud:
`person.jira.<account id>`, or `bot.jira.…` for an app account), announced with the display name
Jira shows for them. The owner is whoever the token belongs to (`/myself`), so list your own account
too, or your events and your issues' levels are `person.jira.<you>` rather than `person.<you>`.

To poll once by hand: `ORCHESTRA_REPO=/path/to/project uv run python -m orchestra_bridges jira --once`.

**Cost.** A round makes about four searches (changed issues, open issues, the owner's open issues,
and hourly the done count), one page per 50 issues (1,000 for the done count), plus one request per
changed issue whose changelog or comments the search cut short.

### When the laptop sleeps

It keeps nothing of its own. On start it reads its own events back from the bus, and the store gives
its last good poll. A poll more than three rounds after the last good one publishes `bridge.gap`
{site, from, to, recovered} and catches up: what was missed is published once, with Jira's times,
back to `max_catchup` at most. While Jira cannot be reached, polls fail and publish nothing. A 429
or 503 is waited out for its `Retry-After` (at most 15 minutes) and tried again.

### Limits, plainly

- Only a comment by the owner answers a comment; a transition or an edit does not.
- `jira.issue.updated` reports sprint, priority, labels, links and due date. Other fields
  (components, fix versions, custom fields) are not published.
- The snapshot fields in each event's data (`status_category`, `labels`, …) are the issue as read,
  not as it was when the change happened.
- Data Center names a new assignee by user key in the changelog; the bridge turns it back into the
  username when the issue's JSON shows that user anywhere, and otherwise publishes the key.
- Searches use JQL's relative minutes (`updated >= "-90m"`), measured by Jira's clock, so a skew
  between the two clocks larger than `overlap` could miss a change.
- Run against a real Jira Cloud site (a team-managed project, 2026-10); not yet against a real Data
  Center. The fake (`tests/fake_jira.py`) follows Atlassian's documented shapes and what the Cloud
  site showed, and the contract test (below) compares them.
- A status change from a status to itself (Jira Cloud writes one just after creating an issue) is
  no transition and is not published. A change between two statuses with the same name but
  different ids (a workflow migration, a move to another project) is published: `from_id` and
  `to_id` differ.

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
  `[[sources]]` table (`preset = "..."`); `rig/orchestra.toml` is an example.
- `orchestra_bridges/rabbitmq/fleet.py`: a simulated fleet for testing it. `orders-api`,
  `payments-api` and `web` publish into three fanout exchanges; `billing` acknowledges in batches
  every 20 s, `shipping` and `notifier` keep up, and `analytics` is too slow at peak, so its
  backlog grows and drains with a 20-minute simulated day.
- `orchestra_bridges/rabbitmq/rig/rig.sh up|down|status`: a RabbitMQ (`orchestra-test-rabbit`:
  AMQP on 127.0.0.1:5682, management on 15682, metrics on 15702), a VictoriaMetrics that scrapes it
  every 5 s (`orchestra-test-rabbit-vm`, 127.0.0.1:8441) and the fleet, whose pid and log are in
  `~/.local/state/orchestra-rabbit-rig/`. The fleet needs only `pika`
  (`uv run --no-project --with pika`).

What the rig shows, in a project made from `rig/orchestra.toml`: the Machines page draws the seven
services as machines known by counts, the three exchanges as channels, double lines with each
rate, and the two unnamed publishers as unattributed. Discovery finds each slow or batched
consumer's backlog as a stock filled by its exchange's publishes and drained by its own acks, the
orders exchange's publishes as one flow with a copy into each other backlog it fills. A consumer
that keeps up (shipping, notifier) leaves a backlog too small to be a stock.

## Develop

```sh
uv sync --extra dev
uv run python -m pytest -q                       # unit and e2e tests, against tests/fake_github*.py
ORCHESTRA_BRIDGES_CONTRACT=1 uv run python -m pytest -q -s tests/contract   # the fakes vs the real API (your gh login)
ORCHESTRA_BRIDGES_SANDBOX=1 uv run python -m pytest -q -s tests/contract/test_github_prs_sandbox.py
    # a real pull request opened, reviewed, labelled, pushed and merged on the private sandbox
    # danroblewis/orchestra-e2e-sandbox (ORCHESTRA_BRIDGES_SANDBOX_REPO to change it)
ORCHESTRA_BRIDGES_JIRA_CONTRACT=1 JIRA_URL=https://jira.acme.com JIRA_API_TOKEN=... \
    JIRA_CONTRACT_JQL='project = ABC' uv run python -m pytest -q -s tests/contract/test_jira_contract.py
    # the fake Jira vs a real one, shapes only, read-only (JIRA_DEPLOYMENT=cloud and JIRA_EMAIL for Cloud)
uv run ruff check .
```

Orchestra comes from `../orchestra_2` (editable). Read `SPEC.md` for the design and `AGENTS.md`
for the rules.
