"""The scripted team: test tooling that plays a team's working day in a Jira test project and a few
Discord test channels, so the read-only pollers (the kits' templates, the demos' collectors) have
something real to relay (the owner, 2026-11-03: "we won't be doing any write-access to Jira or
Slack, so to test it you will need to create and modify Jira tickets and send Discord messages to
test that the read-only features are working").

    python -m orchestra_bridges.team --jira-url https://<site>.atlassian.net --project SCRUM \\
        --channels-from <a discord poller.toml> [--rhythm 20m] [--seed 1] [--dry-run] [--once] [--close]

It is the one part of this package that writes to another system, and it is **never a machine**:
nothing declares it, it announces nothing and publishes nothing on a bus; the pollers relay what it
did as the accounts it used (the token's Jira account, the Discord bot). It runs by hand, never from
the plugin. Every ticket's summary starts ``[orchestra live test]`` and carries the label
``orchestra-live-team``; ``--close`` closes only those, with a resolution, and deletes nothing.

- ``day.py``: the day's script (rounds of acts, seeded);
- ``ledger.py``: what a run created (keys, ids, times; never text), for ``--close`` and an audit;
- ``wire.py``: the HTTP sender (one origin, the token in memory, a write at most every 2 seconds);
- ``jira.py``, ``discord.py``: each system's acts; ``play.py``: the day played, and the close;
- ``settings.py``: the sites, the channels and the credentials (by file reference only)."""
