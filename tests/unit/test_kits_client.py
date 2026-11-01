"""The kits' CachedClient against a kit fake: the cache (hit, miss, expiry, keep, dry run), the
per-host rate limit, retries with Retry-After, and the token kept out of logs, keys, files and errors."""

import json
import logging
import urllib.request

import pytest

from orchestra_bridges.kits.client import (
    CachedClient,
    HTTPFailure,
    KitError,
    NotCached,
    TokenMissing,
    redact,
    retry_after,
)
from orchestra_bridges.kits.fakes import FakePagerDuty, FakePrometheus

TOKEN = "kits-test-token-0123456789"


def ids(body):
    """A keep: the incidents' ids only."""
    return [i["id"] for i in body["incidents"]]


class Clock:
    """A clock that a sleep moves, so the rate limit is tested without waiting."""

    def __init__(self, t: float = 1_793_000_000.0):
        self.t, self.slept = t, []

    def __call__(self) -> float:
        return self.t

    def sleep(self, s: float) -> None:
        self.slept.append(s)
        self.t += s


@pytest.fixture
def fake():
    with FakePagerDuty(token=TOKEN) as f:
        yield f


@pytest.fixture
def make(fake, tmp_path, monkeypatch):
    monkeypatch.setenv("PD_TOKEN", TOKEN)
    clock = Clock()

    def make(**kw):
        kw.setdefault("cache_dir", tmp_path / "cache")
        kw.setdefault("rate_dir", tmp_path / "rate")
        kw.setdefault("per_minute", 6000)
        return CachedClient(fake.url, token_env="PD_TOKEN", auth_format="Token token={token}",
                            sleep=clock.sleep, clock=clock, **kw)
    make.clock = clock
    return make


def test_cache_hit_miss_and_expiry(make, fake):
    api = make(ttl_s=60)
    a = api.get("/incidents", {"statuses[]": ["triggered"]}, keep=ids)
    b = api.get("/incidents", {"statuses[]": ["triggered"]}, keep=ids)
    assert a == b == ["PINC001"] and api.sent == 1 and api.hits == 1
    api.get("/incidents", {"statuses[]": ["acknowledged"]}, keep=ids)    # another URL: a miss
    assert api.sent == 2
    make.clock.t += 61                                                   # past the TTL
    api.get("/incidents", {"statuses[]": ["triggered"]}, keep=ids)
    assert api.sent == 3
    assert len(fake.requests_to("/incidents")) == 3


def test_nothing_is_cached_without_keep(make, tmp_path):
    api = make()
    api.get("/incidents")
    api.get("/incidents")
    assert api.sent == 2 and not list((tmp_path / "cache").rglob("*.json"))


def test_cache_key_has_the_body(tmp_path):
    with FakePrometheus() as prom:
        api = CachedClient(prom.url, cache_dir=tmp_path / "c", rate_dir=tmp_path / "r", per_minute=6000)
        api.post("/api/v1/query", form={"query": "queue_depth"}, keep=lambda b: b)
        api.post("/api/v1/query", form={"query": "queue_depth"}, keep=lambda b: b)
        api.post("/api/v1/query", form={"query": "http_requests_total"}, keep=lambda b: b)
        assert api.sent == 2 and api.hits == 1


def test_keep_decides_what_is_cached(make, tmp_path):
    api = make()
    assert api.get("/incidents", keep=ids) == ["PINC001", "PINC002"]
    text = "".join(p.read_text() for p in (tmp_path / "cache").rglob("*.json"))
    assert "PINC001" in text and "CANARY" not in text and "title" not in text


def test_dry_run_serves_only_the_cache(make, fake):
    make(ttl_s=1).get("/incidents", keep=ids)
    make.clock.t += 3600                                                 # stale, yet a dry run serves it
    dry = make(ttl_s=1, dry_run=True)
    assert dry.get("/incidents", keep=ids) == ["PINC001", "PINC002"]
    with pytest.raises(NotCached):
        dry.get("/incidents/PINC001", keep=lambda b: b["incident"]["id"])
    assert dry.sent == 0 and len(fake.requests) == 1


def test_rate_limit_per_host(make):
    api = make(per_minute=60)                                            # one a second, bursts of 15
    for _ in range(20):
        api.get("/incidents")
    assert api.sent == 20
    assert sum(make.clock.slept) == pytest.approx(5.0, abs=0.1)         # 15 at once, then one a second
    other = make(per_minute=60)                                          # same host, same bucket (its file)
    other.get("/incidents")
    assert sum(make.clock.slept) == pytest.approx(6.0, abs=0.1)


def test_retries_on_429_with_retry_after_and_5xx(make, fake):
    fake.fail_next(429, {"Retry-After": "7"})
    fake.fail_next(503)
    api = make(ttl_s=0, backoff_s=0.5)
    assert api.get("/incidents")["incidents"]
    assert api.sent == 3
    assert 7.0 <= sum(make.clock.slept) < 9.0                          # the hold, then one backoff


def test_gives_up_and_never_retries_a_4xx(make, fake):
    fake.fail_next(500, count=3)                                     # retries=2: three tries, all refused
    with pytest.raises(HTTPFailure) as e:
        make(ttl_s=0, retries=2).get("/incidents")
    assert e.value.status == 500
    fake.fail_next(404)
    api = make(ttl_s=0)
    with pytest.raises(HTTPFailure):
        api.get("/incidents")
    assert api.sent == 1


def test_a_retry_after_too_long_is_not_waited(make, fake):
    fake.fail_next(429, {"Retry-After": "86400"})
    with pytest.raises(HTTPFailure):
        make(ttl_s=0, max_wait_s=60).get("/incidents")


def test_token_never_in_logs_errors_repr_or_files(make, fake, tmp_path, caplog):
    caplog.set_level(logging.DEBUG, logger="orchestra_bridges.kits.client")
    api = make()
    api.get("/incidents", keep=ids)
    api.get("/incidents", keep=ids)
    fake.fail_next(500, count=10)
    with pytest.raises(HTTPFailure) as e:
        make(ttl_s=0, retries=1).get("/incidents", {"api_key": TOKEN})
    assert all(r.authorized for r in fake.requests[:1])
    assert TOKEN not in caplog.text and "Authorization" not in caplog.text and caplog.text
    assert TOKEN not in str(e.value) and TOKEN not in repr(api)
    for p in tmp_path.rglob("*"):
        if p.is_file():
            assert TOKEN not in p.read_text(errors="replace")


def test_only_its_own_origin(make):
    with pytest.raises(ValueError):
        make().get("https://elsewhere.example/steal")


def test_token_needs_https_off_this_machine(monkeypatch, tmp_path):
    monkeypatch.setenv("PD_TOKEN", TOKEN)
    with pytest.raises(ValueError):
        CachedClient("http://api.example.com", token_env="PD_TOKEN", cache_dir=tmp_path, rate_dir=tmp_path)


def test_missing_token_names_the_variable(monkeypatch, tmp_path):
    monkeypatch.delenv("NO_SUCH_TOKEN", raising=False)
    with pytest.raises(TokenMissing, match="NO_SUCH_TOKEN"):
        CachedClient("https://api.example.com", token_env="NO_SUCH_TOKEN", cache_dir=tmp_path, rate_dir=tmp_path)


def test_token_file_must_be_private(monkeypatch, tmp_path):
    f = tmp_path / "tok"
    f.write_text(TOKEN)
    f.chmod(0o644)
    with pytest.raises(TokenMissing, match="chmod 600"):
        CachedClient("https://api.example.com", token_file=f, cache_dir=tmp_path, rate_dir=tmp_path)
    f.chmod(0o600)
    c = CachedClient("https://api.example.com", token_file=f, cache_dir=tmp_path, rate_dir=tmp_path)
    assert TOKEN not in repr(c)


def test_redact_and_retry_after():
    assert "secret" not in redact("https://x/y?api_key=secret&q=1")
    assert "q=1" in redact("https://x/y?api_key=secret&q=1")
    assert retry_after("5", 0) == 5
    assert retry_after("Wed, 21 Oct 2015 07:28:00 GMT", 1445412480 - 10) == pytest.approx(10)
    assert retry_after(None, 0) is None


def test_a_kit_only_reads(make):
    api = make()
    for method, ctype in (("PUT", None), ("DELETE", None), ("POST", "application/json"), ("PATCH", "application/json")):
        with pytest.raises(KitError, match="outbox"):
            api.request(method, "/incidents", body=b"{}", content_type=ctype)
    assert api.sent == 0
    with pytest.raises(TypeError):
        api.post("/incidents", {"title": "x"})              # no JSON body: a form only


def test_a_token_file_inside_the_repository_is_refused(tmp_path, monkeypatch):
    monkeypatch.delenv("PD_TOKEN", raising=False)
    repo = tmp_path / "repo"
    (repo / "secrets").mkdir(parents=True)
    f = repo / "secrets" / "tok"
    f.write_text(TOKEN)
    f.chmod(0o600)
    with pytest.raises(TokenMissing, match="inside the repository"):
        CachedClient("https://api.example.com", token_file=f, cache_dir=tmp_path, rate_dir=tmp_path, repo_root=repo)
    monkeypatch.setenv("PD_TOKEN_FILE", str(tmp_path / "missing"))         # the bridges' reader: $NAME_FILE too
    with pytest.raises(TokenMissing, match="no PD_TOKEN and no token file"):
        CachedClient("https://api.example.com", token_env="PD_TOKEN", cache_dir=tmp_path, rate_dir=tmp_path)


def test_no_cache_outside_a_project(tmp_path, monkeypatch):
    monkeypatch.setenv("ORCHESTRATOR_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("ORCHESTRA_REPO", str(tmp_path / "not-a-project"))
    monkeypatch.chdir(tmp_path)
    with pytest.raises(KitError, match="no project to keep the cache in"):
        CachedClient("https://api.example.com", rate_dir=tmp_path)
    assert not (tmp_path / "home" / "kits").exists()


def test_one_is_local_and_one_env_name():
    from orchestra_bridges import config
    from orchestra_bridges.kits import machinefile, settings
    from orchestra_bridges.local import ENV_NAME, is_local
    assert settings.ENV_NAME is ENV_NAME is config.ENV_NAME and machinefile.is_local is is_local
    assert is_local("http://127.0.0.1:9/x") and is_local("localhost") and is_local("http://[::1]:8/") and not is_local("https://example.com")


def test_basic_auth_joins_the_account_and_the_token(tmp_path):
    """``Basic {basic}``: the token file holds the token as issued (Jira Cloud's API token) and the
    account (its email) comes apart from it; the fake checks base64 of ``account:token``. The fake
    accepts PUT too (a kit never sends one: the outbox's)."""
    from orchestra_bridges.kits.fakes import FakeServer

    class Fake(FakeServer):
        auth_format = "Basic {basic}"

        def route(self, method, path, query, body):
            return 200, {"method": method}

    tfile = tmp_path / "token"
    tfile.write_text(TOKEN + "\n")
    tfile.chmod(0o600)
    with Fake({}, token=TOKEN, basic_user="me@example.invalid") as f:
        api = CachedClient(f.url, token_file=tfile, auth_format="Basic {basic}", basic_user="me@example.invalid",
                           cache_dir=tmp_path / "c", rate_dir=tmp_path / "r", per_minute=6000)
        assert api.get("/x") == {"method": "GET"}
        req = urllib.request.Request(f.url + "/x", data=b"{}", method="PUT",
                                     headers={"Authorization": "Basic " + api._basic, "Content-Type": "application/json"})
        with urllib.request.urlopen(req) as r:
            assert json.loads(r.read()) == {"method": "PUT"}
        assert all(r.authorized for r in f.requests) and len(f.requests) == 2
        wrong = CachedClient(f.url, token_file=tfile, auth_format="Basic {basic}", basic_user="other@example.invalid",
                             cache_dir=tmp_path / "c", rate_dir=tmp_path / "r", per_minute=6000, retries=0)
        with pytest.raises(HTTPFailure):
            wrong.get("/x")
        assert TOKEN not in repr(api)
