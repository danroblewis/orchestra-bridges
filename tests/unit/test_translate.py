import json

from fake_github import CANARY, FakeGitHub

from orchestra_bridges.github.translate import commented, from_event, opened

R = "microsoft/vscode"


def world():
    g = FakeGitHub()
    g.open(1, "alice", 1000.0)
    g.open(2, "bob", 1000.0, pull=True)
    return g


def test_an_issue_is_opened_by_its_author_a_pull_request_is_left_out():
    g = world()
    s = opened(R, g.issues[1])
    assert (s.id, s.type, s.subject, s.login, s.bot) == (f"github:{R}:opened:1", "issue.opened", f"{R}#1", "alice", False)
    assert s.time == "1970-01-01T00:16:40+00:00"
    assert opened(R, g.issues[2]) is None


def test_comments_on_issues_only():
    g = world()
    c = commented(R, g.comment(1, "carol", 1100.0))
    assert (c.type, c.number, c.login) == ("issue.commented", 1, "carol")
    assert c.data["comment"] == g.comments[0]["id"]
    assert commented(R, g.comment(2, "carol", 1100.0)) is None


def test_labels_closes_and_reopens_others_ignored():
    g = world()
    lab = from_event(R, g.event(1, "labeled", "triage[bot]", 1200.0, label="bug", bot=True))
    assert (lab.type, lab.data["label"], lab.bot) == ("issue.labelled", "bug", True)
    closed = from_event(R, g.event(1, "closed", "dave", 1300.0))
    assert (closed.type, closed.data["reason"]) == ("issue.closed", "completed")
    assert from_event(R, g.event(1, "reopened", "alice", 1400.0)).type == "issue.reopened"
    assert from_event(R, g.event(1, "assigned", "dave", 1500.0)) is None
    assert from_event(R, g.event(2, "closed", "dave", 1500.0)) is None          # a pull request


def test_no_text_ever_and_ids_are_stable():
    g = world()
    seen = [opened(R, g.issues[1]), commented(R, g.comment(1, "carol", 1100.0)),
            from_event(R, g.event(1, "labeled", "x", 1200.0, label="bug"))]
    assert CANARY not in json.dumps([s.data for s in seen])
    assert [s.id for s in seen] == [s.id for s in [opened(R, g.issues[1]), commented(R, g.comments[0]),
                                                   from_event(R, g.events[0])]]
