"""What a scaffolded machine's own test reads from its ``poller.toml``, so the test still passes
after the owner's values are set (another token variable, their JQL, their channels): the token's
variable is the one the program will read, and the scope the fake holds is given to the run with
``--set`` (``runner.overrides``) instead of being whatever the template guessed. A fake is started
with the placeholders the runner sends it (``fake_token``, ``fake_user``), never a real token: the
runner reads no credential for a fake on this machine."""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path
from typing import Any

from .machinefile import locate


def poller_toml(here: str | Path) -> dict[str, Any]:
    return tomllib.loads(locate(here).read_text(encoding="utf-8"))


def settings(here: str | Path) -> dict[str, Any]:
    return poller_toml(here).get("settings") or {}


def token_env(here: str | Path, default: str) -> str:
    """The NAME of the variable the program reads its token from (``[settings] token_env``)."""
    return settings(here).get("token_env") or default


def fake_token(here: str | Path) -> str:
    """The token the runner sends a fake on this machine (``[settings] fake_token``, else the kit's)."""
    from .runner import FAKE_TOKEN
    return settings(here).get("fake_token") or FAKE_TOKEN


def fake_user(here: str | Path) -> str:
    """The HTTP Basic account the runner sends a fake (``[settings] fake_user``, else the kit's)."""
    from .runner import FAKE_USER
    return settings(here).get("fake_user") or FAKE_USER


def toml_value(v: Any) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, int | float):
        return repr(v)
    if isinstance(v, str):
        return json.dumps(v)            # a JSON string is a TOML basic string
    if isinstance(v, list | tuple):
        return "[" + ", ".join(toml_value(x) for x in v) + "]"
    if isinstance(v, dict):
        return "{" + ", ".join(f"{json.dumps(str(k))} = {toml_value(x)}" for k, x in v.items()) + "}"
    raise TypeError(f"no TOML for {type(v).__name__}")


def set_args(**values: Any) -> list[str]:
    """``--set`` arguments that give the run these settings (the scope the fake holds)."""
    return [x for k, v in values.items() for x in ("--set", f"{k}={toml_value(v)}")]


def uncleared(here: str | Path, into: str | Path, program: str) -> Path:
    """A copy of the program and its ``poller.toml`` with ``clearance = "needs approval"``, for the
    test that the compliance check refuses a company system: it holds after the owner's approval too."""
    here, into = Path(here), Path(into)
    into.mkdir(parents=True, exist_ok=True)
    src = locate(here)
    text = src.read_text(encoding="utf-8")
    text = re.sub(r'(?m)^clearance\s*=.*$', 'clearance = "needs approval"', text)
    (into / src.name).write_text(text, encoding="utf-8")
    (into / program).write_text((here / program).read_text(encoding="utf-8"), encoding="utf-8")
    return into
