"""Small HTTP fakes of the systems the kits read (GitHub, Jira in both flavours, Slack, PagerDuty, a
Prometheus API), for a machine's tests: each on a random free
loopback port, seeded from a JSON fixture (``fixtures/``, or the test's own), recording what it
received (``requests``), checking a token when given one, and failing on request (``fail_next``).

    with FakePagerDuty(token="t") as pd:
        client = CachedClient(pd.url, token_env="PAGERDUTY_TOKEN", auth_format="Token token={token}", ...)

**A fake of a new system** subclasses ``FakeServer`` (``base.py``) and implements
``route(method, path, query, body) -> (status, json_body)`` in the system's real shapes, its world in
``self.data`` (the fixture: a dict, or a JSON file) and controls of its own to change it between
polls; ``auth_format`` says how the token is sent (``"Bearer {token}"`` by default)::

    class FakeJenkins(FakeServer):
        auth_format = "Basic {token}"
        def route(self, method, path, query, body):
            if path == "/api/json":
                return 200, {"jobs": self.data["jobs"]}
            return 404, None

    with FakeJenkins({"jobs": [...]}, token="dGVzdDp0ZXN0") as f:   # f.url, f.requests, f.fail_next(429)
        ...

Put the canary (``CANARY``) in every text it serves, so a test can show none of it leaves.
``scaffold http`` writes a machine whose test has one."""

from .base import CANARY, FakeServer, Request
from .github import FakeGitHub
from .jira import FakeJira
from .pagerduty import FakePagerDuty
from .prometheus import FakePrometheus
from .slack import FakeSlack

FAKES = {"github": FakeGitHub, "jira": FakeJira, "slack": FakeSlack, "pagerduty": FakePagerDuty, "promql": FakePrometheus}

__all__ = ["CANARY", "FAKES", "FakeGitHub", "FakeJira", "FakePagerDuty", "FakePrometheus", "FakeServer", "FakeSlack", "Request"]
