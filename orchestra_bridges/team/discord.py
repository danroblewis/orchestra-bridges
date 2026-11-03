"""The scripted team's acts in Discord (REST v10, a bot token): a message posted in a channel, an
inline reply to an earlier one, a ✅ reaction on one (the kit's check). Never a delete or an edit."""

from __future__ import annotations

import urllib.parse

from .wire import Wire

API = "/api/v10"
CHECK = "✅"


class Discord:
    def __init__(self, wire: Wire):
        self.wire = wire

    def channel_name(self, channel: str) -> str:
        return (self.wire.call("GET", f"{API}/channels/{channel}") or {}).get("name") or channel

    def post(self, channel: str, text: str, reply_to: str | None = None) -> str:
        body: dict = {"content": text, "allowed_mentions": {"parse": []}}
        if reply_to:
            body["message_reference"] = {"message_id": reply_to, "fail_if_not_exists": False}
        return str(self.wire.call("POST", f"{API}/channels/{channel}/messages", body)["id"])

    def check(self, channel: str, message: str) -> None:
        emoji = urllib.parse.quote(CHECK)
        self.wire.call("PUT", f"{API}/channels/{channel}/messages/{message}/reactions/{emoji}/@me")
