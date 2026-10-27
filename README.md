# orchestra-bridges

Bridges for [Orchestra](https://github.com/danroblewis/orchestra): programs that poll systems
which are buses in all but name and publish what people did there as events on a project's
Orchestra bus. Each event's source is the person (or bot) who acted and its subject is the item
they acted on, so people and bots appear on Orchestra's Machines page beside the programs, and
Orchestra infers who hands work to whom from the subjects. The events hold metadata only, never
text.

The first bridge reads **GitHub issues**. Jira, Slack, Confluence and Google Drive come later
(Orchestra's `docs/proposals/event-sources.md` §4).

## The GitHub issues bridge

Every `every` seconds it reads a repository's issues, issue comments and issue events through the
REST API, and publishes:

| Event | Source | Data (no text) |
|---|---|---|
| `issue.opened` | the author | repository, issue number, login, association |
| `issue.commented` | the commenter | the same, and the comment's id |
| `issue.labelled` | who added the label | the same, and the label's name |
| `issue.closed` | who closed it | the same, and the close reason |
| `issue.reopened` | who reopened it | the same |

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

**Credentials.** It uses `GH_TOKEN` (or `GITHUB_TOKEN`) when one is set, and otherwise runs
`gh auth token` once at start, so your `gh` login is enough. The token stays in memory. It is
never written, logged or published, and it is sent only to the configured API host.

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

## Develop

```sh
uv sync --extra dev
uv run python -m pytest -q                       # unit and e2e tests, against tests/fake_github.py
ORCHESTRA_BRIDGES_CONTRACT=1 uv run python -m pytest -q -s tests/contract   # the fake vs the real API (your gh login)
uv run ruff check .
```

Orchestra comes from `../orchestra_2` (editable). Read `SPEC.md` for the design and `AGENTS.md`
for the rules.
