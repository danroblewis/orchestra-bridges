# orchestra-bridges

Bridges for [Orchestra](https://github.com/danroblewis/orchestra): programs that poll systems
which are buses in all but name and publish what people did there as events on a project's
Orchestra bus. Each event's source is the person (or bot) who acted and its subject is the item
they acted on, so people and bots appear on Orchestra's Machines page beside the programs, and
Orchestra infers who hands work to whom from the subjects. The events hold metadata only, never
text.

The bridges read **GitHub issues** and **Slack pings** (mentions of you and DMs to you, and
whether you answered them). Jira, Confluence and Google Drive come later (Orchestra's
`docs/proposals/event-sources.md` §4).

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

It also writes these series itself, labelled `machine="bridges.github"` (who wrote them, which is
how it finds them again on restart) and `repo`:

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

## The Slack pings bridge

Someone @mentions you or sends you a DM and expects an answer within 15 minutes. This bridge
measures that promise. Every minute it searches your workspace with your own **user token**
(`search.messages`), finds the pings waiting for you, and publishes:

| Event | Source | Subject | Data (no text) |
|---|---|---|---|
| `slack.ping.received` | the pinger | the thread (`<workspace>/<channel>/<thread ts>`) | workspace, channel and its kind, ts, thread ts, member id, kind (`dm`, `mention`, `group`) |
| `slack.ping.answered` | you | the same thread | the same ids, the rule that answered it, the wait (`wait_seconds`) and its bucket (`within`) |
| `slack.ping.expired` | `bridges.slack` | the same thread | the same ids, `reason: expired`, the wait (`wait_seconds`) |

**Answered** means, by default, that you put a checkmark reaction (`white_check_mark` or
`heavy_check_mark`) on the ping. A checkmark also answers the earlier pings still waiting that a
reply there would answer: in a thread, the earlier pings in that thread; outside a thread, the
earlier pings in that channel that were not in a thread either. So a checkmark on the last of five
DM lines answers all five. With `answered = ["reply"]`, your first message after the ping in its
thread answers it instead; a ping that was not in a thread is answered by your next message in the
same channel, also not in a thread. With `answered = ["reaction", "reply"]`, both count. When both
see an answer in the same poll, the reply wins, because a reply has its own time and a reaction
has none.

Under the default rule, a ping you answer only by replying is never answered: it waits until
`expire` (7 days) and counts as expired. Use `answered = ["reaction", "reply"]` if you answer by
replying as often as by checkmark.

A ping nobody answers stops waiting after `expire` and counts as expired. A deleted ping is not
noticed: it too waits until `expire`. So `unanswered = received - answered - expired` changes
balance exactly, while the bus keeps the bridge's events. If reading your reactions fails (a rate
limit, the network, a token without `reactions:read`), the poll goes on: new pings, replies and
expiries are still published, the pings stay waiting and are checked again next poll, and the log
says why, once a poll.

It serves these series at `http://127.0.0.1:<port>/metrics`, each labelled with the workspace.
The plugin declares the instrument that stores them, a scrape named `bridges.slack` every 30 s, so
Orchestra labels each stored point `instrument="bridges.slack"` and lists the scrape on its
Instruments page:

- `slack_pings_unanswered`: the pings waiting for you now (the stock);
- `slack_pings_oldest_unanswered_seconds`: how long the oldest has waited;
- `slack_pings_received_total`, `slack_pings_answered_total{within="5m|15m|60m|over|unknown"}`,
  `slack_pings_expired_total`: what moves the stock. The `within` buckets do not overlap: `15m` is
  more than 5 and at most 15 minutes. `unknown` is a checkmark the bridge first saw more than two
  polls after it could last have seen the ping unanswered (after the laptop slept, or on a first
  start), so its wait is not known. **The promise**, the share answered within 15 minutes, is
  (5m + 15m) / (5m + 15m + 60m + over), with `unknown` left out;
- `slack_ping_wait_seconds_total`: the waits of the answers in the timed buckets, added up. The
  mean wait is its rate divided by the rate of those answers (`unknown` left out of both);
- `slack_bridge_last_poll_timestamp_seconds`: when the last successful poll began (Unix seconds).
  Its age is how stale the counts are; it is absent until the first poll succeeds.

The counters never go down, including across a restart.

**Never text.** Slack's search returns each message's text. The bridge checks it in memory for a
mention of you or your groups, then drops it. Ids, member ids, channels and timestamps are all it
keeps. A test puts a canary string in every fake message and checks that it never reaches the bus,
the metrics, the log or any file.

### Set up

1. **A user token** (`xoxp-...`). Create a Slack app in the workspace
   (api.slack.com/apps → *Create New App* → *From scratch*). Under *OAuth & Permissions* →
   *User Token Scopes*, add:
   - `search:read`, to find the pings and, for the reply rule, your own messages;
   - `reactions:read`, for the checkmark rule (the default).

   `users:read` is not needed: people are named by the handle the search returns, or by the
   identity map. Install the app to the workspace and copy the *User OAuth Token*. A bot token
   (`xoxb-`) cannot search. The bridge only reads; it never posts, reacts or marks anything read.
2. **Where the token goes.** Never in `orchestra.toml` (a `token` key there is refused). Put it in
   a file outside the repository, readable only by you:

   ```sh
   install -d -m 700 ~/.config/orchestra-bridges
   install -m 600 /dev/null ~/.config/orchestra-bridges/slack-token
   ```

   then paste the token into that file. That path is the default; `token_file` in the section, or
   `SLACK_USER_TOKEN_FILE`, names another. The bridge refuses a file inside the repository or one
   others can read.

   The environment variable `SLACK_USER_TOKEN` also works, and suits a poll run by hand
   (`--once`, below). Do not set it where `orchestra run` runs: `orchestra run` passes its
   environment to every process it starts, so every machine would get your token.

   The token stays in memory and is sent only to slack.com (or, in tests, a fake on this machine):
   `api` is refused if it names any other host, or plain http to a host that is not local.
3. **Configure** it in the project's `orchestra.toml`. The section is all it needs: the plugin
   starts the bridge and declares the scrape of its `/metrics` (at `host` and `port`) for you:

```toml
[bridges.slack]
every = "60s"
lookback = "1h"                   # on the very first start, how far back to read
max_catchup = "48h"               # after a gap, how far back it reads at most
overlap = "10m"                   # each poll reads again this far before the last one
answered = ["reaction"]           # or ["reply"], or both; never empty
reactions = ["white_check_mark", "heavy_check_mark"]
groups = []                       # user group ids (S...) whose @mentions are pings too
dms = true                        # whether a DM is a ping
expire = "168h"                   # how long a ping may wait before it stops counting as waiting
host = "127.0.0.1"                # where /metrics listens; keep it local
port = 9465                       # where /metrics is served
# token_file = "~/.config/orchestra-bridges/slack-token"
# queries = ["<@{me}>", "to:<@{me}>"]   # replaces the searches, when your workspace needs others
# api = "https://slack.com/api"   # slack.com, or a local fake in tests
```

A `[[scrape]]` you added by hand before the plugin could declare one still works: a project entry
named `bridges.slack` replaces the plugin's (Orchestra logs that it did), so keep it only to scrape
more often or elsewhere, and delete it otherwise. It must keep the name `bridges.slack`: a
restarted bridge finds its counts by `instrument="bridges.slack"`.

```toml
[[scrape]]
name = "bridges.slack"            # must be this name
url = "http://127.0.0.1:9465/metrics"
every = "10s"
```

With `port = 0` (any free port) the plugin declares no scrape, since it cannot know the port.

Upgrading from before Orchestra's instruments (2026-10-27): the scrape used to label the points
`machine="bridges.slack"`. New points are labelled `instrument="bridges.slack"`, so each series
starts again under the new label; the bridge reads both on restart and continues each counter from
the higher value, so no count is lost or taken down.

In the identity map (`[bridges] people`), a Slack account is a member id or a handle
(`slack = "U012AB3CD"`). List yourself so your answers carry your name and team. Anyone not listed
is `person.slack.<handle>` (`bot.slack.<handle>` for a bot), so a stranger who shares a login with
someone on GitHub is a different machine; only the map joins accounts across systems.

To try one poll by hand:
`SLACK_USER_TOKEN=... ORCHESTRA_REPO=/path/to/project uv run python -m orchestra_bridges slack --once`.

### When the laptop sleeps

As with GitHub: on start, the bridge rebuilds the waiting pings and the counts from its own events.
It then continues each counter from the store's last scraped value, if that is higher. Then it
reads Slack again from where it left off. A poll that comes more than three rounds after the end of
the last good one publishes `bridge.gap` {workspace, from, to, recovered} and catches up. Pings
missed in the gap are published with their Slack times. A restart reads where it left off from the
store: the value of `slack_bridge_last_poll_timestamp_seconds`, not the time of the last scrape,
because Orchestra keeps scraping the bridge while Slack or the network is down. So a restart after
an outage reads the outage's pings back and records the gap. It needs the scrape: without it (with
`port = 0`, or Orchestra not running it), a restart reads from its last event on the bus and records
no gap.

### Limits, plainly

- **Slack does not say when a reaction was added.** A checkmark answer is stamped when the bridge
  first sees it, so it is up to one poll late. Its data says `observed: true` and gives
  `not_before`, the last moment the ping could have been seen unanswered. When that is more than
  two polls back (after a gap, or on a first start), the answer is counted as `unknown`, not timed.
  The reply rule has no such error: a reply has its own time.
- **Waits are measured on the clock**, nights and weekends included. Working hours are not modelled
  (an open question for the owner), so a ping sent at 23:00 and answered at 08:00 waited 9 hours.
- The counts are served, not stamped. A gap's catch-up therefore moves the counters when Orchestra
  next scrapes, not at the times the pings happened. The events keep the true times.
- **Requests.** Each poll makes one `search.messages` call per search per page (three searches by
  default, four with the reply rule, one more per group; a page holds 100 matches) and, while any
  ping waits under the checkmark rule, one to five `reactions.list` calls (a page of 100 of your
  reactions each), however many pings wait. That is well under Slack's limits for those methods
  (about 20 a minute each). A `Retry-After` up to one poll long is waited out; a longer one fails
  that poll, and the next tries again.
- **A checkmark is found while it is among your 500 most recent reactions.** `reactions.list`
  lists the messages you reacted to, most recent first, and the bridge reads five pages. A ping
  whose checkmark has more than 500 later reactions above it by the time the bridge looks (it can
  happen only after a long gap) is not seen as answered, and expires.
- Messages without a member (old-style bot posts, webhooks) are not pings. `@here` and `@channel`
  are not pings either.
- Search syntax: the default searches are `<@you>`, `<!subteam^group>`, `to:<@you>` and
  `from:<@you>`, narrowed with `after:` two days before the window. Slack reads `after:` by day
  in your own time zone, which the bridge does not know; two days back holds in every zone, and
  each match's own time makes the exact cut. The fake reads them as described. Whether a real
  workspace reads them the same way is what the contract test shows (below). `queries` replaces
  them if not.
- Search indexing can lag. Each poll re-reads `overlap` (10 minutes), so a ping indexed late is
  still found.

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
uv run python -m pytest -q                       # unit and e2e tests, against tests/fake_github.py and tests/fake_slack.py
ORCHESTRA_BRIDGES_CONTRACT=1 uv run python -m pytest -q -s tests/contract   # the fake vs the real API (your gh login)
ORCHESTRA_BRIDGES_SLACK_CONTRACT=1 SLACK_CANARY=CANARY-... uv run python -m pytest -q -s tests/contract/test_slack_contract.py
uv run ruff check .
```

The Slack contract test compares the fake's shapes with the real API's using your token. With
`SLACK_CANARY` set, it also checks that the default searches find real pings and that the bridge
never lets their text out. Before running it, have a second account (in a personal workspace, say)
send you a direct message and a mention whose text includes the value of `SLACK_CANARY`
(`CANARY-$(uuidgen)`, say), within the last 48 hours. Set `SLACK_GROUP` to a user group id mentioned
the same way to check the group search too.

Orchestra comes from `../orchestra_2` (editable). Read `SPEC.md` for the design and `AGENTS.md`
for the rules.
