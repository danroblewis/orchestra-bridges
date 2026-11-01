"""Two small rules the bridges and the kits share, kept in one place: what counts as this machine
(a fake on a loopback host, where no token and no approval are needed), and what an environment
variable's name looks like (a setting names a variable, never holds a token)."""

from __future__ import annotations

import ipaddress
import re
import urllib.parse

ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]{1,63}$")


def is_local(url_or_host: str) -> bool:
    """Whether a URL (or a bare host) is on this machine: a loopback address, or ``localhost``."""
    s = str(url_or_host or "")
    host = (urllib.parse.urlsplit(s).hostname if "://" in s else s) or ""
    host = host.lower().strip("[]")
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host == "localhost"
