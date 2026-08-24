"""[P0] RP-03 S1 T3 — Jira DC summary executor contract (held-out oracle).

Specifies the observable contract of the Data Center summary executor defined in ticket
c6f3-cda5-c460-4c5c, consuming the T1 seam (``operation_outcome`` / ``retry_budget``):

- a DEDICATED executor client built through ``jira.JIRA(..., max_retries=0)`` that fails loud
  unless ``client._session.max_retries == 0`` (legacy construction is untouched),
- an injected one-attempt / no-sleep policy on ``_with_connection_retry`` used only for the
  executor's selected calls — every legacy caller keeps three attempts with 2s/5s waits,
- one write invocation that issues exactly GET -> PUT -> GET with no hidden repeats; a failed
  GET or PUT ends the invocation and a successful read-back is authoritative,
- ambiguous-PUT recovery that consumes a SEPARATE physical GET observation and maps through the
  T1 ``decide_replay`` table (desired -> recovered; old_conclusive -> replay within budget /
  exhausted without; failed/inconclusive -> non-replaying ``commit_unknown``),
- no sleep and no replay in the SDK/adapter layer — classification/delay metadata is returned to
  the shared owner instead,
- exactly one redacted completion log per terminal/exhausted outcome, carrying ONLY the seven
  contract fields, its message redacted through the T1 seam and capped at 512 code points, the
  whole serialized log capped at 1,024 code points.

Every clock is injected; these tests perform zero wall-clock sleep.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from rebar_reconciler.adapters.jira_datacenter import retry as _retry
from rebar_reconciler.adapters.jira_datacenter import summary_executor as se
from rebar_reconciler.operation_outcome import (
    DelaySource,
    Disposition,
    FailureScope,
    OperationOutcome,
    ReplaySafety,
)

# ── Fakes ─────────────────────────────────────────────────────────────────────


class _FakeSession:
    def __init__(self, max_retries: int) -> None:
        self.max_retries = max_retries


class _FakeJira:
    """A ``jira.JIRA``-shaped stand-in that records the read/write requests issued against
    it. Never sleeps, never retries internally."""

    def __init__(self, *, max_retries_value: int = 0, summary: str = "old summary") -> None:
        self._session = _FakeSession(max_retries_value)
        self._summary = summary
        self.gets = 0
        self.puts = 0

    def issue(self, remote_id: str) -> _FakeIssue:
        self.gets += 1
        return _FakeIssue(self, remote_id)


class _FakeIssue:
    def __init__(self, client: _FakeJira, remote_id: str) -> None:
        self._client = client
        self.key = remote_id
        self.fields = type("F", (), {"summary": client._summary})()

    def update(self, *, fields: dict[str, Any]) -> None:
        self._client.puts += 1
        self._client._summary = fields["summary"]


def _settings() -> Any:
    return type("S", (), {"url": "https://dc.invalid", "pat": "tok", "ca_bundle": None})()


def _conn_error() -> Exception:
    return ConnectionError("connection reset")


@pytest.fixture
def poison_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Any sleep in the retry seam is a contract violation for executor calls."""
    slept: list[float] = []
    monkeypatch.setattr(_retry.time, "sleep", lambda s: slept.append(s))
    return slept


@pytest.fixture(autouse=True)
def _no_http_error_types(monkeypatch: pytest.MonkeyPatch) -> None:
    # The unit tier has no [jira-datacenter] extra, so the real HTTP-error tuple is empty;
    # keep it empty unless a test opts in, so connection errors take the retry branch.
    monkeypatch.setattr(_retry, "_jira_http_error_types", lambda: ())


# ── Dedicated executor client: the max_retries=0 guard ────────────────────────


def test_executor_client_is_built_with_max_retries_zero():
    captured: dict[str, Any] = {}

    def factory(**kwargs: Any) -> _FakeJira:
        captured.update(kwargs)
        return _FakeJira(max_retries_value=kwargs.get("max_retries", -1))

    client = se.build_executor_client(_settings(), jira_cls=factory)
    assert captured["max_retries"] == 0
    assert client._session.max_retries == 0


def test_executor_client_rejects_a_nonzero_session_retry():
    def factory(**kwargs: Any) -> _FakeJira:
        # A misbehaving client that ignores max_retries=0 and keeps a nonzero session value.
        return _FakeJira(max_retries_value=3)

    with pytest.raises(se.ExecutorClientError):
        se.build_executor_client(_settings(), jira_cls=factory)


# ── GET -> PUT -> GET cardinality; read-back authoritative ────────────────────


def test_one_write_invocation_issues_exactly_get_put_get(poison_sleep):
    client = _FakeJira(summary="old")
    outcome = se.execute_summary_write(client, "DC-1", "new summary")
    assert client.gets == 2  # one read, one authoritative read-back
    assert client.puts == 1
    assert not poison_sleep
    assert isinstance(outcome, OperationOutcome)
    assert outcome.disposition is Disposition.applied
    assert client._summary == "new summary"


def test_failed_put_ends_the_invocation_without_readback(poison_sleep):
    client = _FakeJira(summary="old")
    orig_issue = client.issue

    def issue(remote_id: str) -> Any:
        iss = orig_issue(remote_id)

        def boom(*, fields: dict[str, Any]) -> None:
            raise _conn_error()

        iss.update = boom  # type: ignore[method-assign]
        return iss

    client.issue = issue  # type: ignore[method-assign]
    outcome = se.execute_summary_write(client, "DC-1", "new")
    assert client.gets == 1  # the read-back never happened
    assert outcome.disposition is not Disposition.applied
    assert not poison_sleep


# ── Selected calls use one attempt / no sleep; legacy default unchanged ────────


def test_executor_selected_calls_use_one_attempt_no_sleep(poison_sleep):
    # A connection error on the read GET must NOT be retried by the executor policy: the
    # injected one-attempt/no-sleep policy means it surfaces immediately, no sleep.
    calls = {"n": 0}

    class _Flaky(_FakeJira):
        def issue(self, remote_id: str) -> Any:
            calls["n"] += 1
            raise _conn_error()

    se.execute_summary_write(_Flaky(), "DC-1", "new")
    assert calls["n"] == 1  # exactly one attempt, no retry
    assert not poison_sleep


def test_legacy_with_connection_retry_default_keeps_three_attempts(monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr(_retry.time, "sleep", lambda s: slept.append(s))
    attempts = {"n": 0}

    class _ConnErr(ConnectionError):
        pass

    monkeypatch.setattr(_retry, "_connection_retry_exceptions", lambda: (_ConnErr,))

    def fn() -> None:
        attempts["n"] += 1
        raise _ConnErr("boom")

    with pytest.raises(_ConnErr):
        _retry._with_connection_retry(fn)  # no policy override → legacy default
    assert attempts["n"] == 3
    assert slept == [2, 5]


# ── Ambiguous-PUT recovery → the decide_replay table ──────────────────────────


def test_ambiguous_put_desired_state_recovers_without_replay(poison_sleep):
    client = _FakeJira(summary="new summary")  # observation shows the desired state landed
    outcome = se.observe_after_ambiguous_put(client, "DC-1", "new summary", budget_remaining=True)
    assert client.gets == 1  # exactly one observation request
    assert outcome.disposition is Disposition.recovered
    assert not poison_sleep


def test_ambiguous_put_conclusive_old_replays_only_within_budget(poison_sleep):
    within = se.observe_after_ambiguous_put(
        _FakeJira(summary="old"), "DC-1", "new summary", budget_remaining=True
    )
    assert within.disposition is Disposition.retryable_deferred
    exhausted = se.observe_after_ambiguous_put(
        _FakeJira(summary="old"), "DC-1", "new summary", budget_remaining=False
    )
    assert exhausted.disposition is Disposition.exhausted_transient
    assert not poison_sleep


def test_ambiguous_put_failed_observation_is_non_replaying_commit_unknown(poison_sleep):
    class _FailObserve(_FakeJira):
        def issue(self, remote_id: str) -> Any:
            self.gets += 1
            raise _conn_error()

    outcome = se.observe_after_ambiguous_put(
        _FailObserve(), "DC-1", "new summary", budget_remaining=True
    )
    assert outcome.disposition is Disposition.commit_unknown
    assert outcome.replay_safety is ReplaySafety.forbidden
    assert not poison_sleep


# ── Completion log: reuse the T1 seam; seven fields; bounds; cardinality ───────


def _unknown_outcome() -> OperationOutcome:
    return OperationOutcome(
        logical_id="11111111-1111-4111-8111-111111111111",
        disposition=Disposition.commit_unknown,
        failure_scope=FailureScope.none,
        replay_safety=ReplaySafety.forbidden,
        invocation_count=2,
        request_count=3,
        delay_source=DelaySource.none,
        provider_delay_ms=None,
        retry_not_before=None,
        diagnostics=(),
    )


def test_completion_log_carries_only_the_seven_contract_fields():
    outcome = se.execute_summary_write(_FakeJira(summary="old"), "DC-1", "new")
    log = se.render_completion_log(outcome)
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
    log = se.render_completion_log(_unknown_outcome(), message=secret + " " + "." * 900)
    doc = json.loads(log)
    assert secret not in doc["message"]
    assert len(doc["message"]) <= 512


def test_whole_completion_log_is_capped_at_1024_code_points():
    log = se.render_completion_log(_unknown_outcome(), message="." * 5000)
    assert len(log) <= 1024
