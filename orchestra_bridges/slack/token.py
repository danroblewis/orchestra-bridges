"""The Slack user token: from the environment, or from a file outside the repository.

In order: ``SLACK_USER_TOKEN``; else the file ``SLACK_USER_TOKEN_FILE`` names; else the file
``[bridges.slack] token_file`` names; else ``~/.config/orchestra-bridges/slack-token``. A file
must not be inside the repository (where it could be committed) and must be readable by its owner
only (``chmod 600``), as ssh asks of a key. The token is never in orchestra.toml (``config.py``
refuses a ``token`` key), and it is read once at start and kept in memory only."""

from __future__ import annotations

import os
import stat
from pathlib import Path

DEFAULT_FILE = Path("~/.config/orchestra-bridges/slack-token")


class TokenError(Exception):
    pass


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except ValueError:
        return False


def user_token(repo_root: Path, token_file: Path | None = None) -> str:
    """The owner's Slack user token. Raises, naming where it looked, when there is none."""
    if token := os.environ.get("SLACK_USER_TOKEN", "").strip():
        return token
    named = os.environ.get("SLACK_USER_TOKEN_FILE")
    path = Path(named).expanduser() if named else (token_file or DEFAULT_FILE).expanduser()
    if _inside(path, repo_root):
        raise TokenError(f"{path} is inside the repository: keep the Slack token outside it")
    if not path.exists():
        raise TokenError(f"no SLACK_USER_TOKEN and no token file at {path}")
    mode = path.stat().st_mode
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        raise TokenError(f"{path} is readable by others: chmod 600 it")
    token = path.read_text().strip()
    if not token:
        raise TokenError(f"{path} is empty")
    return token
