"""The scripted team's working day: eight rounds (09:00 to 16:00), each a few acts, drawn from a seed.

Tickets are named ``t1`` … ``t5`` and messages ``m1`` … ``m6`` within the day; the ledger keeps
which key and id each became. A ticket is created, assigned, started, sent to review, done; some are
commented on. In the channels: a status ask at the start and the end of the day, questions and their
answers, an ad-hoc request that becomes a ticket, an incident alert that is answered and checked (✅).
The seed picks the texts, the channels, which acts are optional and the order within a round."""

from __future__ import annotations

import random
import re
from dataclasses import dataclass, field

ROUNDS = 8
PREFIX = "[orchestra live test] "
# within a round: what must come first (a ticket before its assignment, a message before its reply)
RANK = {"create": 0, "post": 0, "assign": 1, "reply": 1, "move": 2, "comment": 2, "check": 2}

WORK = ["Add retry to the export job", "Fix the flaky login test", "Document the deploy checklist",
        "Upgrade the HTTP client library", "Tidy the alert thresholds", "Speed up the nightly report",
        "Remove the unused feature flag", "Add a health check to the worker", "Rotate the staging certificates",
        "Investigate the slow search page"]
QUESTIONS = ["Does anyone know why the staging deploy is queued?", "Who owns the billing dashboard now?",
             "Is the API freeze still on for this week?", "Which branch should hotfixes go to?",
             "Has anyone seen the build cache misses today?"]
ANSWERS = ["Looking into it now.", "I think that's me, checking.", "Yes, until Friday.", "Main, then cherry-pick.",
           "Seen it, a runner was restarted."]
REQUESTS = ["Could someone review the on-call handover notes?", "Can we get a second pair of eyes on the export job?",
            "Could someone bump the worker's memory limit?", "Can someone add me to the release channel?"]
INCIDENTS = ["ALERT: error rate above 5% on checkout-api", "ALERT: queue depth climbing on the export worker",
             "ALERT: p95 latency over 2s on search"]
STATUS = ["Standup: what is everyone on today?", "Status check: anything blocked?",
          "End of day: where did everything land?"]
COMMENTS = ["Picked this up.", "Halfway there, tests next.", "Pushed a draft, needs review.",
            "Blocked on a review, nudging.", "Done on my side."]


@dataclass(frozen=True)
class Act:
    op: str                       # create, assign, move, comment (Jira); post, reply, check (Discord)
    ref: str                      # t<n> or m<n>
    text: str = ""                # a summary, a comment, a message (the team's own, synthetic)
    kind: str = ""                # Task or Story; the stage (start, review, done); a message's kind
    channel: int = 0              # which of the channels (an index; a reply's and a check's follow their message)
    who: int = 0                  # which of the assignable people (an index, modulo how many there are)

    @property
    def system(self) -> str:
        return "jira" if self.op in ("create", "assign", "move", "comment") else "discord"

    def refs(self) -> list[str]:
        """The tickets (``{t5}``) the text names, which must exist before it is sent."""
        return re.findall(r"\{(t\d+)\}", self.text)


@dataclass
class Day:
    seed: int
    rounds: list[list[Act]] = field(default_factory=list)


def day(seed: int, channels: int = 1) -> Day:
    rng = random.Random(seed)
    ch = lambda: rng.randrange(max(1, channels))
    work = rng.sample(WORK, 5)
    q, r, inc = rng.sample(range(len(QUESTIONS)), 2), rng.choice(REQUESTS), rng.choice(INCIDENTS)

    def create(n):
        return Act("create", f"t{n}", PREFIX + work[n - 1], rng.choice(["Task", "Task", "Story"]))

    def comment(n):
        return [Act("comment", f"t{n}", rng.choice(COMMENTS))] if rng.random() < 0.7 else []

    def assign(n):
        return Act("assign", f"t{n}", who=rng.randrange(100))

    plan = [
        [create(1), create(2), create(3), assign(1), assign(2),
         Act("post", "m1", STATUS[0], "status", ch()), Act("post", "m2", QUESTIONS[q[0]], "question", ch())],
        [Act("move", "t1", kind="start"), *comment(1), Act("reply", "m2", ANSWERS[q[0]], "answer"),
         Act("post", "m3", r, "request", ch()), create(4)],
        [Act("move", "t2", kind="start"), assign(3), Act("check", "m2", kind="check"),
         Act("post", "m4", inc, "incident", ch())],
        [Act("move", "t1", kind="review"), *comment(2), Act("reply", "m4", "On it, rolling back.", "answer"), assign(4)],
        [Act("move", "t1", kind="done"), Act("move", "t3", kind="start"), Act("check", "m4", kind="check"),
         create(5), Act("reply", "m3", "Opened {t5} for it.", "answer")],
        [Act("move", "t2", kind="review"), *comment(3), Act("post", "m5", QUESTIONS[q[1]], "question", ch()), assign(5)],
        [Act("move", "t2", kind="done"), Act("move", "t4", kind="start"), Act("check", "m3", kind="check"),
         Act("reply", "m5", ANSWERS[q[1]], "answer")],
        [Act("move", "t3", kind="review"), Act("post", "m6", STATUS[2], "status", ch()), *comment(4)],
    ]
    for acts in plan:
        rng.shuffle(acts)
        acts.sort(key=lambda a: RANK[a.op])
    return Day(seed, plan)
