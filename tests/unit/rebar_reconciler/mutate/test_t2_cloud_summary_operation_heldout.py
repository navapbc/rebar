"""[P0] RP-03 S1 T2 — Cloud summary operation core contract (held-out oracle).

Independent held-out pins on the mechanism of the Cloud summary operation defined in ticket
a8cd-01cd-c370-454f, consuming the T1 seam (``operation_outcome`` / ``retry_budget``). This
suite deliberately scopes to the parts that fake cleanly and carry the contract's weight — it is
NOT the whole AC set (the one-process-per-write launch, historical-triple decoding, and POSIX /
non-POSIX timeout cleanup are exercised by the implementation's own state tests):

- the per-call retry policy is a PASS-THROUGH that leaves every existing caller unchanged: the
  default path of ``_rest_urlopen_with_retry`` / ``_direct_rest_get`` / ``get_issue_by_rest``
  still makes three attempts with 2s/5s waits, and ONLY a caller that opts in with
  ``ONE_ATTEMPT_NO_SLEEP`` gets a single attempt with no sleep (this is the blocking-review fix:
  outbound_differ / apply_handlers / binding_store keep the default),
- summary recovery observes the PRIMARY store with exactly one REST request and no inner sleep,
  then maps the observation through the T1 ``decide_replay`` table,
- REST error classification splits permanent (auth/HTTP) from transient (network) without the
  adapter itself sleeping or replaying,
- exactly one redacted completion log per terminal/exhausted outcome, seven fields, its message
  redacted through the T1 seam and capped at 512 code points, the whole log capped at 1,024.

Every clock is injected; these tests perform zero wall-clock sleep.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

import pytest

import rebar_reconciler.adapters.jira.acli_rest as _acli_rest
from rebar_reconciler.adapters.jira import acli
from rebar_reconciler.adapters.jira import summary_operation as so
from rebar_reconciler.operation_outcome import (
    DelaySource,
    Disposition,
    FailureScope,
    OperationOutcome,
    ReplaySafety,
)

# ── Fakes / harness ───────────────────────────────────────────────────────────


def _client() -> acli.AcliClient:
    return acli.AcliClient(
        jira_url="https://example.atlassian.net",
        user="u",
        api_token="t",
        jira_project="DIG",
    )


@pytest.fixture
def capture_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    slept: list[float] = []
    monkeypatch.setattr(_acli_rest.time, "sleep", lambda s: slept.append(s))
    return slept


def _install_flaky_urlopen(monkeypatch: pytest.MonkeyPatch, counter: dict[str, int]) -> None:
    """Make the innermost urlopen always raise a transient connection fault, counting attempts."""

    def _flaky(req: Any, timeout: int = 10) -> Any:
        counter["n"] += 1
        raise ConnectionError("connection reset")

    monkeypatch.setattr(_acli_rest.urllib.request, "urlopen", _flaky)


# ── Per-call policy is a pass-through: default callers are UNCHANGED ───────────


def test_rest_urlopen_default_still_makes_three_attempts(capture_sleep, monkeypatch):
    counter = {"n": 0}
    _install_flaky_urlopen(monkeypatch, counter)
    client = _client()
    req = urllib.request.Request("https://example.atlassian.net/rest/api/3/issue/DIG-1")
    with pytest.raises(ConnectionError):
        client._rest_urlopen_with_retry(req)  # no policy → legacy default
    assert counter["n"] == 3
    assert capture_sleep == [2, 5]


def test_direct_rest_get_default_still_makes_three_attempts(capture_sleep, monkeypatch):
    counter = {"n": 0}
    _install_flaky_urlopen(monkeypatch, counter)
    client = _client()
    with pytest.raises(ConnectionError):
        client._direct_rest_get("/rest/api/3/issue/DIG-1")  # no policy → legacy default
    assert counter["n"] == 3
    assert capture_sleep == [2, 5]


def test_get_issue_by_rest_default_still_makes_three_attempts(capture_sleep, monkeypatch):
    counter = {"n": 0}
    _install_flaky_urlopen(monkeypatch, counter)
    client = _client()
    with pytest.raises(ConnectionError):
        client.get_issue_by_rest("DIG-1")  # the three external consumers call it exactly like this
    assert counter["n"] == 3
    assert capture_sleep == [2, 5]


def test_get_issue_by_rest_one_attempt_policy_makes_one_attempt_no_sleep(
    capture_sleep, monkeypatch
):
    counter = {"n": 0}
    _install_flaky_urlopen(monkeypatch, counter)
    client = _client()
    with pytest.raises(ConnectionError):
        client.get_issue_by_rest("DIG-1", retry_policy=so.ONE_ATTEMPT_NO_SLEEP)
    assert counter["n"] == 1  # exactly one attempt
    assert capture_sleep == []  # no inner sleep


# ── Summary recovery: one REST request → decide_replay ────────────────────────


class _FakeRestClient:
    """Stands in for AcliClient at the get_issue_by_rest seam only."""

    def __init__(self, *, summary: str | None = None, raise_exc: Exception | None = None) -> None:
        self._summary = summary
        self._raise = raise_exc
        self.get_calls: list[dict[str, Any]] = []

    def get_issue_by_rest(self, jira_key: str, *, retry_policy: Any = None) -> dict[str, Any]:
        self.get_calls.append({"key": jira_key, "policy": retry_policy})
        if self._raise is not None:
            raise self._raise
        return {"fields": {"summary": self._summary}}


def test_observe_desired_state_recovers_with_one_request(capture_sleep):
    client = _FakeRestClient(summary="new summary")
    outcome = so.observe_summary_via_rest(client, "DIG-1", "new summary", budget_remaining=True)
    assert len(client.get_calls) == 1  # exactly one primary-store REST request
    assert client.get_calls[0]["policy"] is so.ONE_ATTEMPT_NO_SLEEP  # single-attempt recovery
    assert outcome.disposition is Disposition.recovered
    assert capture_sleep == []


def test_observe_conclusive_old_replays_only_within_budget(capture_sleep):
    within = so.observe_summary_via_rest(
        _FakeRestClient(summary="old"), "DIG-1", "new summary", budget_remaining=True
    )
    assert within.disposition is Disposition.retryable_deferred
    exhausted = so.observe_summary_via_rest(
        _FakeRestClient(summary="old"), "DIG-1", "new summary", budget_remaining=False
    )
    assert exhausted.disposition is Disposition.exhausted_transient
    assert capture_sleep == []


def test_observe_failed_rest_is_non_replaying_commit_unknown(capture_sleep):
    client = _FakeRestClient(raise_exc=ConnectionError("reset"))
    outcome = so.observe_summary_via_rest(client, "DIG-1", "new summary", budget_remaining=True)
    assert outcome.disposition is Disposition.commit_unknown
    assert outcome.replay_safety is ReplaySafety.forbidden
    assert capture_sleep == []


# ── REST error classification: permanent vs transient, no adapter replay ──────


def test_permanent_http_error_classifies_as_permanent_failure():
    exc = urllib.error.HTTPError(
        url="https://example.atlassian.net/rest/api/3/issue/DIG-1",
        code=401,
        msg="Unauthorized",
        hdrs=None,  # type: ignore[arg-type]
        fp=None,
    )
    disposition = so.classify_rest_error(exc)
    assert disposition is Disposition.permanent_failure


def test_transient_network_error_classifies_as_retryable_deferred():
    disposition = so.classify_rest_error(ConnectionError("connection reset"))
    assert disposition is Disposition.retryable_deferred


# ── Completion log: reuse the T1 seam; seven fields; bounds ───────────────────


def _unknown_outcome() -> OperationOutcome:
    return OperationOutcome(
        logical_id="22222222-2222-4222-8222-222222222222",
        disposition=Disposition.commit_unknown,
        failure_scope=FailureScope.none,
        replay_safety=ReplaySafety.forbidden,
        invocation_count=1,
        request_count=1,
        delay_source=DelaySource.none,
        provider_delay_ms=None,
        retry_not_before=None,
        diagnostics=(),
    )


def test_completion_log_carries_only_the_seven_contract_fields():
    log = so.render_completion_log(_unknown_outcome())
    doc = json.loads(log)
    assert set(doc) == {
        "logical_id",
        "disposition",
        "invocation_count",
        "request_count",
        "cleanup_status",
        "retry_not_before",
        "message",
    }


def test_completion_log_message_is_redacted_and_capped_at_512_code_points():
    secret = "sk-ant-deadbeefdeadbeefdeadbeefdeadbeef"
    log = so.render_completion_log(_unknown_outcome(), message=secret + " " + "." * 900)
    doc = json.loads(log)
    assert secret not in doc["message"]
    assert len(doc["message"]) <= 512


def test_whole_completion_log_is_capped_at_1024_code_points():
    log = so.render_completion_log(_unknown_outcome(), message="." * 5000)
    assert len(log) <= 1024
