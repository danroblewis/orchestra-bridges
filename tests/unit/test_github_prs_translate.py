from fake_github_prs import FakePRs

from orchestra_bridges.github_prs.translate import (
    check_run,
    from_issue_event,
    opened,
    opening_head,
    pull,
    reviewed,
    status,
)

R = "acme/service"


def world():
    g = FakePRs(R)
    g.open(1, "alice", 1_000_000.0, branch="agent/a", committer="alice")
    return g


def test_opened_names_the_branch_and_head_and_no_text():
    g = world()
    p = pull(R, g.pulls[1])
    s = opened(p)
    assert (s.id, s.type, s.subject, s.login) == (f"github:{R}:pr:1:opened", "github.pr.opened", f"{R}#1", "alice")
    assert s.data == {"repository": R, "pull": 1, "login": "alice", "branch": "agent/a", "base": "main",
                      "head": g.head(1), "association": "MEMBER", "draft": False}


def test_the_head_at_opening_is_the_last_commit_dated_before_it():
    g = world()
    first = g.head(1)
    g.push(1, "alice", 1_000_100.0)
    p = pull(R, g.pulls[1])
    assert opening_head(p, g.timelines[1]) == first != p.head


def test_a_review_carries_its_state_and_comment_count_and_its_comments_their_place():
    g = world()
    rid = g.review(1, "bob", 1_000_050.0, "CHANGES_REQUESTED", comments=(("a.py", 3),))
    p = pull(R, g.pulls[1])
    entry = next(e for e in g.timelines[1] if e.get("event") == "reviewed")
    review, comment = reviewed(p, entry, g.line_comments[1])
    assert (review.data["state"], review.data["comments"], review.id) == ("changes_requested", 1, f"github:{R}:pr:1:review:{rid}")
    assert {k: comment.data[k] for k in ("path", "line", "side", "subject_type", "in_reply_to")} == {
        "path": "a.py", "line": 3, "side": "RIGHT", "subject_type": "line", "in_reply_to": None}
    assert comment.time == review.time                                     # visible when the review was submitted
    assert reviewed(p, {**entry, "state": "pending"}, []) == []


def test_issue_events_labels_and_requests():
    g = world()
    g.label(1, "SAIL", "alice", 1_000_010.0)
    g.request_review(1, "alice", "bob", 1_000_020.0)
    p = pull(R, g.pulls[1])
    lab, req = (from_issue_event(p, e) for e in g.timelines[1][1:])
    assert (lab.type, lab.data["label"]) == ("github.pr.labelled", "SAIL")
    assert (req.type, req.data["reviewer"], req.login) == ("github.pr.review_requested", "bob", "alice")
    assert from_issue_event(p, g.timelines[1][0]) is None                    # a commit is not an issue event


def test_checks_only_when_done():
    g = world()
    g.check(1, "build", None, 1_000_030.0)
    g.check(1, "build", "success", 1_000_040.0, app="circleci-checks")
    g.status(1, "ci/x", "pending", "bot", 1_000_050.0)
    p = pull(R, g.pulls[1])
    running, done = (check_run(p, r) for r in g.runs[g.head(1)])
    assert running is None and (done.login, done.bot, done.data["conclusion"]) == ("circleci-checks[bot]", True, "success")
    assert status(p, g.statuses[g.head(1)][0], g.head(1)) is None
