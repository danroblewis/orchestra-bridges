"""A scaffolded machine's ``machine.toml``: what its own program reads (``[settings]``) and the
compliance check it runs before any read.

``[settings]`` holds the API's base URL, the NAME of the environment variable with its token (never
the token: ``read`` refuses a ``token`` key) or ``token_file``, a file outside any repository
(``chmod 600``) holding it, the poll interval, the rate and the cache TTL, ``ca_file`` (a CA bundle
trusted beside the system's, behind a re-signing proxy) and ``fake_token``/``fake_user`` (the
placeholders sent to a fake on this machine instead of any credential: ``runner.py``). An optional
``[notes]`` table holds what the machine says of itself on its card (``notes.py``: ``stand_in``,
``disabled``). The
compliance check (``require_clearance``): reading another system's data is the owner's to approve.
Until ``clearance = "approved"`` is in ``machine.toml`` (the Supervisor writes it on the owner's
word, with ``data_class``), the machine reads only a fake on this machine (a loopback host) and
refuses anything else before a request is made (machine-design.md §2.2, "Company systems")."""

from __future__ import annotations

import ipaddress
import tomllib
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from orchestra.config import parse_duration

from . import notes as note
from .settings import ENV_NAME

CLEARANCES = ("needs approval", "approved")


class MachineFileError(ValueError):
    pass


@dataclass
class MachineFile:
    name: str
    kit: str
    base_url: str
    token_env: str | None
    token_file: str | None = None
    user_env: str | None = None         # HTTP Basic's account ("Basic {basic}"): a variable's name
    user_file: str | None = None        # or a chmod 600 file outside any repository holding it
    every_s: float = 300.0
    per_minute: int = 60
    ttl_s: float = 240.0
    clearance: str = "needs approval"
    data_class: str = "unknown"
    ca_file: str | None = None          # a PEM bundle trusted beside the system's CAs
    fake_token: str | None = None       # what a run against a fake sends instead of the token (runner.FAKE_TOKEN)
    fake_user: str | None = None        # and instead of the HTTP Basic account (runner.FAKE_USER)
    notes: dict[str, str] = field(default_factory=dict)      # [notes]: stand_in, disabled (notes.py)
    extra: dict[str, Any] = field(default_factory=dict)      # the kit's own settings (jql, channels, queries)
    poll_dir: Path | None = None        # where its poller keeps its cursor and counters (set by the runner)


def read(path: str | Path) -> MachineFile:
    p = Path(path)
    try:
        raw = tomllib.loads(p.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError) as e:
        raise MachineFileError(f"{p}: {e}") from None
    s = raw.get("settings") or {}
    if "token" in s:
        raise MachineFileError(f"{p}: [settings] token: a token never goes in a file here; name its variable in token_env")
    env = s.get("token_env")
    if env is not None and not (isinstance(env, str) and ENV_NAME.match(env)):
        raise MachineFileError(f"{p}: [settings] token_env must be the NAME of an environment variable")
    tfile = s.get("token_file")
    if tfile is not None and not (isinstance(tfile, str) and tfile):
        raise MachineFileError(f"{p}: [settings] token_file must be a path (to a file outside any repository)")
    uenv, ufile = s.get("user_env"), s.get("user_file")
    if uenv is not None and not (isinstance(uenv, str) and ENV_NAME.match(uenv)):
        raise MachineFileError(f"{p}: [settings] user_env must be the NAME of an environment variable")
    if ufile is not None and not (isinstance(ufile, str) and ufile):
        raise MachineFileError(f"{p}: [settings] user_file must be a path (to a file outside any repository)")
    for k in ("ca_file", "fake_token", "fake_user"):
        if s.get(k) is not None and not (isinstance(s[k], str) and s[k]):
            raise MachineFileError(f"{p}: [settings] {k} must be a string")
    try:
        notes = note.check(raw.get("notes"), "[notes]")
    except note.NoteError as e:
        raise MachineFileError(f"{p}: {e}") from None
    clearance = str(raw.get("clearance", "needs approval"))
    if clearance not in CLEARANCES:
        raise MachineFileError(f"{p}: clearance must be one of {CLEARANCES}")
    known = {"base_url", "token_env", "token_file", "user_env", "user_file", "every", "per_minute", "ttl", "ca_file",
             "fake_token", "fake_user"}
    return MachineFile(
        name=str(raw.get("name") or p.parent.name), kit=str(raw.get("kit") or ""),
        base_url=str(s.get("base_url") or ""), token_env=env, token_file=tfile, user_env=uenv, user_file=ufile,
        every_s=parse_duration(s.get("every"), 300.0), per_minute=int(s.get("per_minute", 60)),
        ttl_s=parse_duration(s.get("ttl"), 240.0), clearance=clearance,
        data_class=str(raw.get("data_class") or "unknown"), ca_file=s.get("ca_file"),
        fake_token=s.get("fake_token"), fake_user=s.get("fake_user"), notes=notes,
        extra={k: v for k, v in s.items() if k not in known})


def is_local(url: str) -> bool:
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == "localhost"


def require_clearance(m: MachineFile, base_url: str) -> None:
    """Raise unless ``base_url`` may be read: a fake on this machine, or an approved system."""
    if is_local(base_url) or m.clearance == "approved":
        return
    raise MachineFileError(
        f"{m.name}: reading {urllib.parse.urlsplit(base_url).hostname} is not cleared (machine.toml clearance = "
        f"{m.clearance!r}, data_class = {m.data_class!r}). The owner approves reading a company system; then "
        "clearance = \"approved\". Until then it runs only against its fake.")
