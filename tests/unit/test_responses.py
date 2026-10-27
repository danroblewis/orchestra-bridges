from orchestra_bridges.github.responses import Responses, within


def balance(r: Responses) -> bool:
    return r.opened - sum(r.answered.values()) - r.closed_unanswered == len(r.waiting)


def test_someone_elses_comment_answers_the_author_s_does_not():
    r = Responses()
    assert r.fold("issue.opened", "i#1", "person.alice", 0.0)
    assert not r.fold("issue.commented", "i#1", "person.alice", 60.0)
    assert r.fold("issue.commented", "i#1", "person.bob", 600.0)
    assert r.answered == {"15m": 1} and r.response_seconds == 600.0 and not r.waiting
    assert not r.fold("issue.commented", "i#1", "person.carol", 700.0)       # only the first response counts
    assert balance(r)


def test_labels_never_answer_and_bots_only_when_asked():
    r = Responses()
    r.fold("issue.opened", "i#1", "person.alice", 0.0)
    assert not r.fold("issue.labelled", "i#1", "person.bob", 10.0)
    assert not r.fold("issue.commented", "i#1", "bot.triage", 10.0)
    assert Responses(bots_answer=True).fold("issue.opened", "i#1", "person.alice", 0.0)
    rb = Responses(bots_answer=True)
    rb.fold("issue.opened", "i#1", "person.alice", 0.0)
    assert rb.fold("issue.commented", "i#1", "bot.triage", 10.0) and rb.answered == {"15m": 1}


def test_closing_answers_unless_it_is_the_author_or_an_uncounted_bot():
    r = Responses()
    for n, closer in ((1, "person.bob"), (2, "person.alice"), (3, "bot.stale")):
        r.fold("issue.opened", f"i#{n}", "person.alice", 0.0)
        r.fold("issue.closed", f"i#{n}", closer, 7200.0)
    assert r.answered == {"1d": 1} and r.closed_unanswered == 2 and balance(r)


def test_issues_not_seen_opened_are_not_counted():
    r = Responses()
    assert not r.fold("issue.commented", "i#9", "person.bob", 0.0)
    assert not r.fold("issue.closed", "i#9", "person.bob", 0.0)


def test_buckets_and_points():
    assert [within(s) for s in (900, 901, 3600, 86400, 86401)] == ["15m", "1h", "1h", "1d", "over"]
    r = Responses()
    r.fold("issue.opened", "i#1", "person.alice", 100.0)
    r.fold("issue.opened", "i#2", "person.alice", 200.0)
    pts = {(p.series, p.labels.get("within")): p.value for p in r.points(400.0, {"repo": "x"})}
    assert pts[("github_issues_unanswered", None)] == 2
    assert pts[("github_issues_unanswered_oldest_seconds", None)] == 300.0
    assert pts[("github_issues_answered_total", "over")] == 0
