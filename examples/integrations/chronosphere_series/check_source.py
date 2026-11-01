"""Check a ``[[sources]]`` entry for a Prometheus-API store (Chronosphere) before the Supervisor adds
it: the entry is accepted, the store answers, the declared series come back, and the label aliases
apply (no series carries a spelling ``[sources.labels]`` renames). Read-only, through Orchestra's
own sources code (``orchestra.sources``: the same reads discovery will make, under the entry's
limits); nothing is stored and no credential is printed.

    python check_source.py [--sources sources.toml] [--url URL] [--window 600] [--state-dir DIR]

``--sources`` is the entry to check (this directory's ``sources.toml`` by default); ``--project``
checks the entry as the project's configuration has it instead (once added). ``--url`` points it
at another server (a fake's, in a test). The compliance check comes first, as in the kit's
programs: until ``poller.toml`` says ``clearance = "approved"``, only a fake on this machine is
read. A fake needs no credentials: with the token's variable unset, a loopback URL is read without
``auth``. Exit 0: all good; 1: a check failed (each named); 2: refused before any read."""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
import time
import tomllib
from pathlib import Path
from types import SimpleNamespace

from orchestra.sources.catalogue import Catalogue
from orchestra.sources.report import check
from orchestra.sources.spec import load

from orchestra_bridges.kits.machinefile import MachineFileError, is_local, read, require_clearance

HERE = Path(__file__).resolve().parent


def entry(args) -> tuple[dict, Path]:
    """The raw ``[[sources]]``/``[[series]]`` tables to check, and the repository they resolve against."""
    if args.project:
        from orchestra.config import load_project
        project = load_project()
        raw = project.config.raw
        return {"sources": raw.get("sources") or [], "series": raw.get("series") or []}, project.repo_root
    raw = tomllib.loads(Path(args.sources).read_text(encoding="utf-8"))
    return {"sources": raw.get("sources") or [], "series": raw.get("series") or []}, Path.cwd()


def pointed(raw: dict, url: str | None) -> dict:
    """Each source at ``url`` when given; a fake on this machine without credentials it does not need."""
    for s in raw["sources"]:
        if url:
            s["url"] = url
        env = (s.get("auth") or {}).get("bearer_env")
        if is_local(s["url"]) and env and not os.environ.get(env):
            s.pop("auth", None)
    return raw


def aliases_apply(spec, series: list[tuple[str, dict]]) -> list[str]:
    """What is wrong with the read series' labels: a spelling the aliases rename, still there."""
    renamed = {real for canonical, spellings in spec.labels.items() for real in spellings if real != canonical}
    return [f"{name} still has {sorted(set(labels) & renamed)} (the aliases rename them)"
            for name, labels in series if set(labels) & renamed]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="check_source.py", description=__doc__.split("\n\n")[0])
    ap.add_argument("--sources", default=str(HERE / "sources.toml"), help="the entry to check (a TOML file)")
    ap.add_argument("--project", action="store_true", help="check the project's own [[sources]] instead")
    ap.add_argument("--url", help="read this server instead of the entry's url (a fake's)")
    ap.add_argument("--window", type=float, default=600.0, help="seconds of the declared series to read back")
    ap.add_argument("--state-dir", help="where the reads are cached for the check (a temporary directory otherwise)")
    a = ap.parse_args(argv)
    try:
        raw, repo = entry(a)
        raw = pointed(raw, a.url)
        m = read(HERE / "poller.toml")
        if not raw["sources"]:
            print("no [[sources]] to check", file=sys.stderr)
            return 2
        for s in raw["sources"]:
            require_clearance(m, s["url"])
    except (OSError, tomllib.TOMLDecodeError, MachineFileError, KeyError) as e:
        print(f"check_source: {e}", file=sys.stderr)
        return 2
    specs = load(raw, repo)
    problems = [f"{name}: refused: {why}" for name, why in specs.refused.items()]
    with tempfile.TemporaryDirectory() as tmp:
        state = Path(a.state_dir or tmp)
        cat = Catalogue(SimpleNamespace(key="check", repo_root=repo, state_dir=state), None, specs)
        for line in check(cat):
            print(line)
        for spec in specs.sources:
            if not cat.remotes[spec.name].health().ok:
                problems.append(f"{spec.name}: the store does not answer its query API")
                continue
            now = time.time()
            reading = cat.read(now - a.window, now, 30.0, keep=False)
            got = [(name, labels) for name, labels, pts in reading.dump if pts and name.startswith(f"{spec.prefix}:")]
            st = reading.status.get(spec.name) or {}
            print(f"{spec.name}: {len(got)} series read (max_series {spec.max_series}), aliases "
                  + ", ".join(f"{'|'.join(v)} -> {k}" for k, v in spec.labels.items()))
            for name, labels in sorted(got, key=lambda x: (x[0], sorted(x[1].items()))):
                print(f"  {name} {{{', '.join(f'{k}={v}' for k, v in sorted(labels.items()))}}}")
            if spec.name in reading.missing:
                problems.append(f"{spec.name}: {reading.missing[spec.name]}")
            elif not got:
                problems.append(f"{spec.name}: no series came back (is the match or a [[series]] query wrong?)")
            if st.get("refused_series"):
                problems.append(f"{spec.name}: series refused: {st['refused_series']}")
            problems += [f"{spec.name}: {p}" for p in aliases_apply(spec, got)]
    for p in problems:
        print(f"PROBLEM {p}")
    print("ok: the store answers and the aliases apply" if not problems else f"{len(problems)} problem(s)")
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
