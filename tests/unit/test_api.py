import pytest
from fake_github import TOKEN, FakeGitHub

from orchestra_bridges.github.api import GitHub, GitHubError, _next_link


def test_pages_follow_the_link_header_with_the_token():
    with FakeGitHub(per_page_max=2) as g:
        for n in range(1, 6):
            g.open(n, "alice", 1000.0 + n)
        api = GitHub(TOKEN, g.base)
        pages = list(api.pages("/repos/microsoft/vscode/issues", {"state": "all", "sort": "updated", "direction": "asc"}))
        assert [[i["number"] for i in p] for p in pages] == [[1, 2], [3, 4], [5]]
        assert [r[0] for r in g.requests] == ["/repos/microsoft/vscode/issues"] + ["/repositories/41881900/issues"] * 2
        assert {r[2] for r in g.requests} == {f"Bearer {TOKEN}"}
        assert api.rate_remaining == 4997


def test_retry_after_is_waited_out():
    waits = []
    with FakeGitHub() as g:
        g.fail_next = [(403, {"Retry-After": "7"}), (429, {"Retry-After": "3"})]
        api = GitHub(TOKEN, g.base, sleep=waits.append)
        assert list(api.pages("/repos/microsoft/vscode/issues/events", {})) == [[]]
        assert waits == [7.0, 3.0]


def test_a_primary_limit_waits_for_its_reset_only_when_it_is_near():
    with FakeGitHub() as g:
        waits = []
        g.fail_next = [(403, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "1100"})]
        api = GitHub(TOKEN, g.base, sleep=waits.append, clock=lambda: 1000.0)
        list(api.pages("/repos/microsoft/vscode/issues/events", {}))
        assert waits == [101.0]
        g.fail_next = [(403, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": "99999"})]
        with pytest.raises(GitHubError, match="HTTP 403"):
            list(api.pages("/repos/microsoft/vscode/issues/events", {}))


def test_failures_name_the_path_never_the_token():
    with FakeGitHub() as g:
        with pytest.raises(GitHubError) as e:
            list(GitHub("wrong-token-xyz", g.base).pages("/repos/microsoft/vscode/issues", {}))
        assert "HTTP 401" in str(e.value) and "wrong-token-xyz" not in str(e.value)
        assert "wrong-token-xyz" not in repr(GitHub("wrong-token-xyz", g.base))


def test_the_token_never_goes_to_another_host():
    api = GitHub(TOKEN, "https://api.github.com")
    with pytest.raises(GitHubError, match="refusing"):
        api._get("https://evil.example/repos/x/y/issues")


def test_next_link():
    assert _next_link('<https://a/b?page=2>; rel="next", <https://a/b?page=9>; rel="last"') == "https://a/b?page=2"
    assert _next_link('<https://a/b?page=1>; rel="prev"') is None
    assert _next_link(None) is None


class _Recorder:
    """A server that answers 302 to ``target`` (when given), else an empty list, recording each
    request's headers."""

    def __init__(self, target: str | None = None):
        import threading
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
        self.seen: list[dict] = []
        rec = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_GET(self):
                rec.seen.append(dict(self.headers))
                if target:
                    self.send_response(302)
                    self.send_header("Location", target + self.path)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"[]")

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def test_a_redirect_to_another_port_gets_no_token():
    elsewhere = _Recorder()
    redirector = _Recorder(target=elsewhere.base)
    try:
        with pytest.raises(GitHubError, match="refusing to follow a redirect") as e:
            list(GitHub(TOKEN, redirector.base).pages("/repos/microsoft/vscode/issues", {}))
    finally:
        redirector.close()
        elsewhere.close()
    assert redirector.seen[0]["Authorization"] == f"Bearer {TOKEN}"
    assert elsewhere.seen == [] and TOKEN not in str(e.value)


@pytest.mark.parametrize("api, host, variable", [("https://api.github.com", "github.com", "GH_TOKEN"),
                                                 ("https://ghe.example.com/api/v3", "ghe.example.com", "GH_ENTERPRISE_TOKEN")])
def test_the_token_is_the_one_for_the_apis_host(monkeypatch, api, host, variable):
    import subprocess

    from orchestra_bridges.github.api import gh_token
    for v in ("GH_TOKEN", "GITHUB_TOKEN", "GH_ENTERPRISE_TOKEN", "GITHUB_ENTERPRISE_TOKEN"):
        monkeypatch.delenv(v, raising=False)
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout=f"token-for-{cmd[-1]}\n", stderr="")
    monkeypatch.setattr("orchestra_bridges.github.api.subprocess.run", run)
    assert gh_token(api) == f"token-for-{host}"
    assert calls == [["gh", "auth", "token", "--hostname", host]]
    monkeypatch.setenv("GH_TOKEN", "the-github-com-token")
    assert gh_token(api) == ("the-github-com-token" if host == "github.com" else f"token-for-{host}")
    monkeypatch.setenv(variable, "from-the-environment")
    assert gh_token(api) == "from-the-environment"
