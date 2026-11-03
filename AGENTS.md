# Rules for working on the bridges

Read `SPEC.md` first. It is the design. This file is the short list of rules that must not be
broken without updating the spec and saying so in the commit.

## Invariants

1. **A library, never a reader.** The package reads no system itself, runs no process, declares
   no instrument and no machine (`plugin.py`: the guide and the check only). Every collector is a
   script scaffolded from a kit into the owner's repository (or the state directory, when asked),
   run by the project's own `[[machines]]` and `[[collect]]` entries. The direct readers were
   retired on 2026-11-01; never bring one back here. (The scripted team, rule 3's exception, is
   test tooling run by hand, not a reader.)
2. **Never text.** A collector publishes, caches, logs and prints metadata only: ids, logins,
   numbers, states, label names, times. Never titles, bodies, comments, messages or document
   content. Text may pass through memory, and nowhere else. Every template's test and every fake
   keeps the canary.
3. **Read only.** The kits' client sends GET, or a form POST that asks a question, and refuses
   anything else. A write to another system is an Outbox draft, never a kit's. The fakes accept
   writes only so a test can change their world. The one exception is test tooling, never a kit:
   the scripted team (`orchestra_bridges/team/`, SPEC §7) writes to the owner's Jira test project
   and Discord test channels only, run by hand, never by the plugin, never a machine (2026-11-03).
4. **Credentials by reference.** A token comes from a `chmod 600` file outside any repository or
   an environment variable a setting names (`secret.read_token`, the one reader), is kept in
   memory, and is sent only to the configured host, never to a fake on this machine (the runner
   sends placeholders there). It is never written, logged, cached, printed or put in a test.
5. **Clearance.** A scaffolded script reads only a fake on this machine until its `poller.toml`
   says `clearance = "approved"`, which only the owner's approval writes.
6. **A relay, never a machine** (Orchestra's rule 2, D19). A poller publishes only as the person or
   bot who acted (`kits/poller.py` refuses any other source), announces each of them once (with
   its notes: `kits/notes.py`), never announces itself and never publishes as its own name. Its
   measurement is an instrument and publishes nothing.
7. **Through Orchestra's library only.** Events go through `orchestra.events.Events`; settings are
   read from the project's merged configuration, never by opening a file (Orchestra's rule 10).
   Nothing in the package imports Orchestra's web app, model or host, and nothing in Orchestra
   knows a bridge (Orchestra's rule 1).
8. **What the scaffold writes is the reference.** `examples/integrations/scaffolded/` is exactly
   the templates' output; change a template, regenerate it, and keep each kit's own test passing
   against its fake with the owner's values in.

## Conventions

- Python 3.12+, `uv` for everything. Run tests with `uv run python -m pytest -q` and lint with
  `uv run ruff check .`.
- Each kit has a fake of its API in the real shapes (`kits/fakes/`) and a template whose test runs
  against it.
- Keep modules small and single-purpose. If a file needs a section divider comment, split it into
  two files. Comments and docs use plain language and are honest about limits.
- Orchestra comes from GitHub (`[tool.uv] override-dependencies`, `main`), at the
  commit `uv.lock` pins; README, "Developing against a local Orchestra".
