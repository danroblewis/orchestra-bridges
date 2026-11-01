# chronosphere_series

Chronosphere's series (any Prometheus-API store: Prometheus, Mimir, Thanos, VictoriaMetrics).
A metrics store has no item events: this publishes nothing and runs no poller.

## Preferred: a `[[sources]]` entry (`sources.toml`)

Orchestra reads the store in place at each discovery (its SPEC §4.4): nothing is copied, nothing
runs. The entry names the token's variable (`auth = { bearer_env = "CHRONOSPHERE_TOKEN" }`), a
prefix (`chrono:`), **label aliases** (`[sources.labels]`: `cluster = ["k8s_cluster", "cluster"]`,
so the store's `k8s_cluster` reads as `cluster`), **limits** (`max_series = 50`, `max_per_family`,
`rate`, `max_requests_per_day`), `infer = false` (only the declared `[[series]]`, never a listing
of the tenant) and two `[[series]]`: `queue_depth` (a level) and `http_requests` (a counter).

Check it before it is added:

```bash
python check_source.py                # the entry in sources.toml (refused until cleared, below)
python check_source.py --url URL      # against another server (a fake's)
python check_source.py --project      # the entry as the project's configuration has it, once added
```

It prints what Orchestra's own `orchestra sources check` prints (the auth by kind and where it
comes from, never its value; whether the store answers), then reads the declared series through
`orchestra.sources` and lists them with their labels; exit 0 when the entry is accepted, the store
answers, series came back and no series still carries a spelling the aliases rename. It reads
nothing until `poller.toml` says `clearance = "approved"` (a fake on this machine aside). Then the
Supervisor adds the tables through `config_edit` (the overlay by default).

## Fallback: a collector (`chronosphere_series.py measure`)

When a series must be computed or stored here: each `[[settings.queries]]` in `poller.toml` once
(instant, at now), printed as a gauge named by its `name` with the answer's labels (aliases
applied); an answer over `max_series` fails the round. Its `[[collect]]` entry:

```toml
[[collect]]
name = "chronosphere_series"
command = "python .orchestra/machines/chronosphere_series/chronosphere_series.py measure"
every = "5m"
timeout = "30s"
```

Test: `uv run python -m pytest examples/integrations/chronosphere_series -q`.
