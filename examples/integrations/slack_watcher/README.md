# slack_watcher

Polls a few Slack channels (`[settings] channels`, ids) every 5 minutes for their new messages
(`conversations.history`), and follows the mentions of a few members (`watch`) until they answer.

## What it emits

`slack.message.posted`, once per message (id `slack_watcher:slack.message.posted:<channel>/<ts>@posted`),
with `<channel>/<thread ts>` as the subject (a reply's thread, or the message's own ts) and **who
posted it** as the source: `person.slack.<member id>`, or `bot.slack.<id>` for a bot. Data: `channel`,
`reply`, `bot`, `mentions` (the watched members it mentions, ids), `answers` (a watched member
answering a mention of them in the thread).

**Mention filtering:** `publish = "mentions"` publishes only the messages that mention a watched
member and those members' answers; `"all"` (the default) publishes every message. Either way every
message is counted. A mention is found in the text in memory (`<@U001>`); only the ids are kept.

## What it measures

`python slack_watcher.py measure` prints, for its `[[collect]]` entry, from what the poller counted
(kept with its cursor, so a restart neither loses nor repeats a count):

- `slack_messages_total{channel}` (counter): messages posted in the channel;
- `slack_mentions_total`, `slack_mentions_answered_total` (counters): mentions of a watched member
  opened, and answered in their thread;
- `slack_mentions_open_total` (gauge): the mentions not yet answered (a level: the two counters
  above are its flows).

## Run it

As `jira_poller` (its README): copy the directory, set `channels` and `watch`, export
`SLACK_TOKEN` (`channels:history`, `users:read`), the owner's clearance, then the `[[machines]]`
entry (`python <dir>/slack_watcher.py`, `restart = "always"`) and the `[[collect]]` entry
(`python <dir>/slack_watcher.py measure`, every 60 s) through `config_edit`.

Test: `uv run python -m pytest examples/integrations/slack_watcher -q`.
