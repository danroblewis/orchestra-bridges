"""A scripted working day in five Slack channels with response policies, for ``FakeSlack.play``:
threads started (by people, and in ``#incidents`` by an alerting bot), answered within the
channel's policy or late or never, checked with the check emoji or not, and one kind checked then
unchecked. Deterministic: thread ``i`` is of kind ``KINDS[i % 6]`` in channel ``CHANNELS[i % 5]``,
so 30 threads hold each kind once in each channel, and ``Day.expected`` says what each kind adds up
to.

Times are seconds from the day's start (09:00): thread ``i`` starts at ``600 + 900 i`` (every
quarter of an hour until 16:25); a reply comes at a fraction of the channel's policy (in time) or
past it (late); a check twenty minutes after the reply.

| Kind | The first reply by another | The check emoji |
|---|---|---|
| ``in_time`` | at 0.4 of the policy | 20 min later |
| ``late`` | at 1.6 of the policy | 20 min later |
| ``in_time_unchecked`` | at 0.5 of the policy | never |
| ``never_answered`` | never | never |
| ``bump_then_in_time`` | the author bumps it at 0.2 (not an answer), another at 0.7 | 20 min later |
| ``checked_then_unchecked`` | at 0.3 of the policy | 15 min later, removed 15 min after |
"""

from __future__ import annotations

from dataclasses import dataclass, field

CHECK = "white_check_mark"
DAY_S = 9 * 3600.0                       # 09:00 to 18:00
# (id, name, the first-reply policy in seconds)
CHANNELS = (("C0SREHELP", "sre-help", 7200.0), ("C0ONCALL", "oncall", 1800.0),
            ("C0INCIDENT", "incidents", 900.0), ("C0INFRA", "infra-requests", 14400.0),
            ("C0DBHELP", "db-help", 3600.0))
ASKERS = ("U101", "U102", "U103", "U104", "U105", "U106")
RESPONDERS = ("U201", "U202", "U203")
ALERTER = "B001"                          # the alerting bot that opens #incidents' threads
KINDS = ("in_time", "late", "in_time_unchecked", "never_answered", "bump_then_in_time", "checked_then_unchecked")
CHECK_AFTER_S = 1200.0
OUTCOMES = {   # what each kind adds up to: the first reply, and whether a check holds at the end
    "in_time": ("in_time", True), "late": ("late", True), "in_time_unchecked": ("in_time", False),
    "never_answered": (None, False), "bump_then_in_time": ("in_time", True),
    "checked_then_unchecked": ("in_time", False),
}


@dataclass
class Day:
    actions: list[dict] = field(default_factory=list)   # {"at", "op", "channel", "thread", "user", ...}
    threads: list[dict] = field(default_factory=list)   # {"n", "channel", "name", "kind", "author", "at"}

    @property
    def length_s(self) -> float:
        return max(a["at"] for a in self.actions)

    def world(self) -> dict:
        """The fake's fixture for this day: the channels (empty until played), their names, the people."""
        users = [{"id": u, "name": u.lower(), "real_name": f"CANARY-TEXT-NEVER-LEAVES {u}", "is_bot": False,
                  "tz": "America/Denver"} for u in (*ASKERS, *RESPONDERS)]
        users.append({"id": ALERTER, "name": "alerts", "real_name": "CANARY-TEXT-NEVER-LEAVES alerts",
                      "is_bot": True, "tz": "UTC"})
        return {"users": users, "channels": {c: [] for c, _, _ in CHANNELS},
                "names": {c: name for c, name, _ in CHANNELS}}

    def expected(self, channel: str | None = None) -> dict[str, int]:
        """What the day adds up to (in one channel's name, or all): threads started, the first reply
        by another in time, late or never, checked at the end or not, and checks removed."""
        out = dict.fromkeys(("started", "answered_in_time", "answered_late", "unanswered", "checked",
                             "never_checked", "unchecked"), 0)
        for t in self.threads:
            if channel not in (None, t["name"], t["channel"]):
                continue
            reply, checked = OUTCOMES[t["kind"]]
            out["started"] += 1
            out[{"in_time": "answered_in_time", "late": "answered_late", None: "unanswered"}[reply]] += 1
            out["checked" if checked else "never_checked"] += 1
            out["unchecked"] += t["kind"] == "checked_then_unchecked"
        return out


def policies() -> dict[str, float]:
    return {name: s for _, name, s in CHANNELS}


def day(n: int = 30) -> Day:
    d, per = Day(), {}
    for i in range(n):
        ch, name, policy = CHANNELS[i % len(CHANNELS)]
        kind = KINDS[i % len(KINDS)]
        k = per[ch] = per.get(ch, -1) + 1
        author = ALERTER if name == "incidents" else ASKERS[i % len(ASKERS)]
        other = RESPONDERS[i % len(RESPONDERS)]
        at = 600.0 + 900.0 * i
        d.threads.append({"n": k, "channel": ch, "name": name, "kind": kind, "author": author, "at": at})
        def act(when, op, user, at=at, ch=ch, k=k, **x):
            d.actions.append({"at": at + when, "op": op, "channel": ch, "thread": k, "user": user, **x})

        act(0.0, "start", author, bot=author == ALERTER)
        reply = {"in_time": 0.4, "late": 1.6, "in_time_unchecked": 0.5, "bump_then_in_time": 0.7,
                 "checked_then_unchecked": 0.3}.get(kind)
        if kind == "bump_then_in_time" and author != ALERTER:
            act(0.2 * policy, "reply", author)
        if reply is None:
            continue
        act(reply * policy, "reply", other)
        if kind in ("in_time", "late", "bump_then_in_time"):
            act(reply * policy + CHECK_AFTER_S, "react", other, emoji=CHECK)
        elif kind == "checked_then_unchecked":
            act(reply * policy + 900.0, "react", other, emoji=CHECK)
            act(reply * policy + 1800.0, "unreact", other, emoji=CHECK)
    d.actions.sort(key=lambda a: a["at"])
    return d
