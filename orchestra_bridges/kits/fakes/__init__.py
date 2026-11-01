"""Small HTTP fakes of the systems the kits read, for a machine's tests: each on a random free
loopback port, seeded from a JSON fixture (``fixtures/``, or the test's own), recording what it
received (``requests``), checking a token when given one, and failing on request (``fail_next``).

    with FakePagerDuty(token="t") as pd:
        client = CachedClient(pd.url, token_env="PAGERDUTY_TOKEN", auth_format="Token token={token}", ...)
"""

from .base import CANARY, FakeServer, Request
from .jira import FakeJira
from .pagerduty import FakePagerDuty
from .prometheus import FakePrometheus
from .slack import FakeSlack

FAKES = {"jira": FakeJira, "slack": FakeSlack, "pagerduty": FakePagerDuty, "promql": FakePrometheus}

__all__ = ["CANARY", "FAKES", "FakeJira", "FakePagerDuty", "FakePrometheus", "FakeServer", "FakeSlack", "Request"]
