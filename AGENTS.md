# Rules for working on the bridges

Read `SPEC.md` first. It is the design. This file is the short list of rules that must not be
broken without updating the spec and saying so in the commit.

## Invariants

1. **Never text.** A bridge publishes, writes and logs metadata only: ids, logins, numbers,
   states, label names, times. It never publishes titles, bodies, comments, messages or document
   content. Text may pass through memory, and nowhere else. The canary test must stay green for
   every bridge.
2. **Read only.** A bridge never writes to the system it reads: no comments, labels or reactions.
   Speaking into another system is a separate proposal (the outbox).
3. **The token stays in memory.** A bridge takes credentials at start from the environment, a
   token file outside the repository readable only by its owner, or the owner's CLI login. It never writes, logs or publishes them, and sends them only to the
   configured API host.
4. **No state of its own.** What a bridge knows comes from its own events on the bus and its own
   series in the metrics store, rebuilt at start. Ids are the system's own, so re-reading never
   duplicates.
5. **Through Orchestra's library only.** Events go through `orchestra.events.Events` and metrics
   through `orchestra.metrics`. Processes and machines are declared through `orchestra.plugins` and
   `orchestra.launcher.Process`. Nothing here imports Orchestra's web app, model or host, and
   nothing in Orchestra knows a bridge (Orchestra's rule 1).
6. **Sources are actors.** An event's source is the person or bot who acted (`identity.py`), and
   its subject is the item. A system's own doing has the bridge's machine as its source.
7. **Catch up, never assume continuity.** After a gap, a bridge reads back to where it left off
   (within `max_catchup`) and records the gap on the bus.

## Conventions

- Python 3.12+, `uv` for everything. Run tests with `uv run python -m pytest -q` and lint with
  `uv run ruff check .`.
- Each bridge gets a fake of its API in the real shapes (`tests/fake_<system>.py`) and an opt-in
  contract test against the real API that compares shapes only.
- Keep modules small and single-purpose. If a file needs a section divider comment, split it into
  two files. Comments and docs use plain language and are honest about limits.
- Orchestra comes from `../orchestra_2` (editable, `[tool.uv.sources]`).
