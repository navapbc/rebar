"""The STEP_JQL_SEARCH visibility wait must be an env-tunable capped-exponential backoff.

Jira Cloud's Lucene search index is eventually consistent: a create + property write
land synchronously (the by-key read right after succeeds), but a ``labels="..."`` JQL
search may not reflect the new issue for tens of seconds. The old fixed ``6 x 5s`` budget
(~25s of waiting) timed out under that lag against live REB, failing the probe for a
reason that is NOT a bridge defect. The wait is now a capped-exponential backoff whose
attempt count, base, and cap are env-tunable, so the default budget covers the observed
lag with margin and an operator can widen it without a code change.

These tests drive ``run_access_check`` directly with a fake client and a recording
``sleep_fn``, asserting the OBSERVABLE schedule (the sequence of sleeps and the number of
search attempts) — not any private schedule-builder.
"""

from __future__ import annotations

import pytest

from rebar._lib_ops import _engine_module

pytestmark = pytest.mark.unit


class _NeverVisibleClient:
    """A probe client whose JQL search NEVER returns a hit — forces every retry.

    Every other step succeeds so the probe reaches STEP_JQL_SEARCH and exhausts the
    whole backoff schedule.
    """

    def __init__(self, **_kwargs) -> None:
        self.issue_key = "DIG-1"
        self.property_value: str | None = None
        self.search_calls = 0

    def create_issue(self, _fields):
        return {"key": self.issue_key}

    def _direct_rest_put_raw(self, _path, _body):
        return None

    def set_issue_property(self, _key, _name, value):
        self.property_value = value

    def search_issues(self, _jql):
        self.search_calls += 1
        return []

    def get_issue_property(self, _key, _name):
        return self.property_value

    def delete_issue(self, _key):
        return None


def _base_env(**overrides: str) -> dict[str, str]:
    env = {
        "JIRA_URL": "https://example.atlassian.net",
        "JIRA_USER": "operator@example.com",
        "JIRA_API_TOKEN": "secret",
        "JIRA_PROJECT": "DIG",
    }
    env.update(overrides)
    return env


def test_default_backoff_is_capped_exponential_over_ten_attempts() -> None:
    """Defaults: 10 attempts, base 3s, cap 30s → sleeps [3,6,12,24,30,30,30,30,30]."""
    access_check = _engine_module("rebar_reconciler.access_check")
    client = _NeverVisibleClient()
    delays: list[float] = []

    result, _lines, returncode = access_check.run_access_check(
        env=_base_env(),
        client_cls=lambda **_kw: client,
        sleep_fn=delays.append,
    )

    # 10 attempts when every attempt returns empty (one search per attempt).
    assert client.search_calls == 10
    # Nine between-attempt sleeps following min(base * 2**k, cap).
    assert delays == [3, 6, 12, 24, 30, 30, 30, 30, 30]
    # The lagging search still yields a FAIL verdict (no masking of a real miss).
    assert returncode == 1
    assert result["verdict"] == "FAIL"


def test_backoff_is_recomputed_from_env_overrides() -> None:
    """JIRA_PROBE_JQL_{RETRIES,SLEEP,SLEEP_MAX} recompute the schedule at call time."""
    access_check = _engine_module("rebar_reconciler.access_check")
    client = _NeverVisibleClient()
    delays: list[float] = []

    env = _base_env(
        JIRA_PROBE_JQL_RETRIES="5",
        JIRA_PROBE_JQL_SLEEP="2",
        JIRA_PROBE_JQL_SLEEP_MAX="10",
    )

    access_check.run_access_check(
        env=env,
        client_cls=lambda **_kw: client,
        sleep_fn=delays.append,
    )

    # 5 attempts → 4 sleeps: min(2 * 2**k, 10) = [2, 4, 8, 10].
    assert client.search_calls == 5
    assert delays == [2, 4, 8, 10]


def test_backoff_stops_early_when_the_index_becomes_visible() -> None:
    """Once the search returns a hit, no further attempts or sleeps happen."""
    access_check = _engine_module("rebar_reconciler.access_check")

    class _VisibleOnThirdAttempt(_NeverVisibleClient):
        def search_issues(self, _jql):
            self.search_calls += 1
            if self.search_calls >= 3:
                return [{"key": self.issue_key}]
            return []

    client = _VisibleOnThirdAttempt()
    delays: list[float] = []

    result, _lines, returncode = access_check.run_access_check(
        env=_base_env(),
        client_cls=lambda **_kw: client,
        sleep_fn=delays.append,
    )

    # Visible on the 3rd attempt → exactly 2 between-attempt sleeps, then success.
    assert client.search_calls == 3
    assert delays == [3, 6]
    assert returncode == 0
    assert result["verdict"] == "PASS"


def test_invalid_env_values_fall_back_to_defaults() -> None:
    """Non-numeric / non-positive overrides are ignored (fail safe to the defaults)."""
    access_check = _engine_module("rebar_reconciler.access_check")
    client = _NeverVisibleClient()
    delays: list[float] = []

    env = _base_env(
        JIRA_PROBE_JQL_RETRIES="not-a-number",
        JIRA_PROBE_JQL_SLEEP="0",
        JIRA_PROBE_JQL_SLEEP_MAX="",
    )

    access_check.run_access_check(
        env=env,
        client_cls=lambda **_kw: client,
        sleep_fn=delays.append,
    )

    assert client.search_calls == 10
    assert delays == [3, 6, 12, 24, 30, 30, 30, 30, 30]
