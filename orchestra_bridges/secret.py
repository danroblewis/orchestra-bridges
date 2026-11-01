"""The bridges' one token reader, for the kits' client and runner: from the environment, or from
a file outside the repository.

In order: the variable ``<NAME>`` (say ``JIRA_API_TOKEN``); else the file ``<NAME>_FILE`` names;
else the file a ``machine.toml``'s ``token_file`` names; else, when given, a default file under
``~/.config/orchestra-bridges/``. A file must not be inside the repository (where it could be
committed) and must be readable by its owner only (``chmod 600``), as ssh asks of a key. The token
is read once and kept in memory only; errors name where it looked, never what it found."""

from __future__ import annotations

import os
import stat
from pathlib import Path

CONFIG_DIR = Path("~/.config/orchestra-bridges")


class TokenError(Exception):
    pass


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def read_token(name: str | None, default_file: str | None, repo_root: Path | None,
               token_file: str | Path | None = None) -> str | None:
    """The token in ``$name``, else in the file ``$name_FILE``, ``token_file`` or the default names.
    Raises, naming where it looked but never what it found, when there is none or the file is unsafe
    (inside ``repo_root``, readable by others, empty). None only when nothing is named at all (no
    variable, no file, no default): a reader that needs no token."""
    if name and (token := os.environ.get(name, "").strip()):
        return token
    named = os.environ.get(f"{name}_FILE") if name else None
    given = named or token_file or (CONFIG_DIR / default_file if default_file else None)
    if given is None:
        if name:
            raise TokenError(f"the environment variable {name} is not set")
        return None
    path = Path(given).expanduser()
    if repo_root is not None and _inside(path, repo_root):
        raise TokenError(f"{path} is inside the repository: keep the token outside it")
    if not path.exists():
        raise TokenError(f"no {name} and no token file at {path}" if name else f"no token file at {path}")
    if path.stat().st_mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise TokenError(f"{path} is readable by others: chmod 600 it")
    token = path.read_text().strip()
    if not token:
        raise TokenError(f"{path} is empty")
    return token
