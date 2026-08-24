"""[P0] REB-3115 S1 T2 — Jira Cloud summary operation contract (author-written).

Covers the parts the held-out oracle does NOT: exactly-one-ACLI-process per write
(AC1), the transient/permanent classification breadth (AC4), historical-triple
decoding + unknown-shape fail-loud driven off the sanitized corpus (AC5), bounded +
redacted diagnostics (AC7), and the completion-log field set / bounds / single-log
cardinality (AC8). Also pins the per-call one-attempt/no-sleep policy pass-through
that leaves every unrelated REST caller on three attempts with 2s/5s waits (AC3).

Everything here is deterministic: the ACLI subprocess seam (``acli_subprocess.
_run_acli``) and the urllib retry seam (``_rest_urlopen_with_retry``) are replaced
by recorders, so no subprocess spawns and nothing sleeps on the wall clock.
"""

from __future__ import annotations

import json
import subprocess
import urllib.error
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from rebar_reconciler.adapters.jira import acli, acli_subprocess
from rebar_reconciler.adapters.jira import summary_operation as so
from rebar_reconciler.adapters.jira.acli_rest import ONE_ATTEMPT_NO_SLEEP
from rebar_reconciler.operation_outcome import Disposition, OperationOutcome, ReplaySafety

_CORPUS = (
    Path(__file__).resolve().parents[3] / "fixtures" / "reconciler" / "rp03_acli_outcomes.json"
)


def _fake_client() -> SimpleNamespace:
    """A minimal stand-in carrying only the attributes the write path reads."""
    return SimpleNamespace(_acli_cmd=["acli"], _call_timeout=None)


# ── AC1: one Cloud write launches EXACTLY ONE ACLI process ─────────────────────


def test_one_write_launches_exactly_one_acli_edit_process(monkeypatch):
    calls: list[list[str]] = []

    def fake_run_acli(cmd: list[str], **_kwargs: Any) -> SimpleNamespace:
        calls.append(cmd)
        return SimpleNamespace(stdout=json.dumps({"key": "PROJ-1", "successCount": 1}))

    monkeypatch.setattr(acli_subprocess, "_run_acli", fake_run_acli)
    outcome = so.execute_cloud_summary_write(_fake_client(), "PROJ-1", "new title")

    assert len(calls) == 1, "exactly one ACLI process per write invocation"
    cmd = calls[0]
    assert cmd[:3] == ["jira", "workitem", "edit"]
    assert cmd[cmd.index("--key") + 1] == "PROJ-1"
    assert cmd[cmd.index("--summary") + 1] == "new title"
    assert isinstance(outcome, OperationOutcome)
    assert outcome.disposition is Disposition.applied
    assert outcome.invocation_count == 1
    assert outcome.request_count == 1
    assert outcome.replay_safety is ReplaySafety.not_applicable


def test_write_passes_retry_on_timeout_false(monkeypatch):
    """A WRITE is never blind-retried on timeout (Jira is non-idempotent)."""
    seen: dict[str, Any] = {}

    def fake_run_acli(cmd: list[str], **kwargs: Any) -> SimpleNamespace:
        seen.update(kwargs)
        return SimpleNamespace(stdout=json.dumps({"successCount": 1}))

    monkeypatch.setattr(acli_subprocess, "_run_acli", fake_run_acli)
    so.execute_cloud_summary_write(_fake_client(), "PROJ-2", "s")
    assert seen.get("retry_on_timeout") is False


# ── AC4: write-fault classification maps into the shared contract ──────────────


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (acli_subprocess.AcliAuthError(["acli"], "unauthorized"), Disposition.permanent_failure),
        (acli_subprocess.AcliMutationError("edit FAILURE"), Disposition.permanent_failure),
        (
            acli_subprocess.AcliTimeoutError(["acli"], 1.0),
            Disposition.commit_unknown,
        ),
        (subprocess.CalledProcessError(1, ["acli"], "", "boom"), Disposition.retryable_deferred),
        (RuntimeError("transient blip"), Disposition.retryable_deferred),
    ],
)
def test_write_error_classification(monkeypatch, exc: BaseException, expected: Disposition):
    def fake_run_acli(cmd: list[str], **_kwargs: Any) -> SimpleNamespace:
        raise exc

    monkeypatch.setattr(acli_subprocess, "_run_acli", fake_run_acli)
    outcome = so.execute_cloud_summary_write(_fake_client(), "PROJ-3", "s")
    assert outcome.disposition is expected
    assert outcome.invocation_count == 1
    # A timed-out (ambiguous) write forbids replay; a clean transient does not.
    if expected is Disposition.commit_unknown:
        assert outcome.replay_safety is ReplaySafety.forbidden
    else:
        assert outcome.replay_safety is ReplaySafety.not_applicable


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (urllib.error.HTTPError("u", 401, "unauthorized", {}, None), Disposition.permanent_failure),
        (urllib.error.HTTPError("u", 403, "forbidden", {}, None), Disposition.permanent_failure),
        (urllib.error.HTTPError("u", 400, "invalid", {}, None), Disposition.permanent_failure),
        (urllib.error.HTTPError("u", 500, "server", {}, None), Disposition.permanent_failure),
        (ConnectionError("reset"), Disposition.retryable_deferred),
        (TimeoutError("read timed out"), Disposition.retryable_deferred),
        (urllib.error.URLError(TimeoutError("t")), Disposition.retryable_deferred),
        (urllib.error.URLError(ConnectionError("c")), Disposition.retryable_deferred),
        (urllib.error.URLError("certificate verify failed"), Disposition.permanent_failure),
    ],
)
def test_classify_rest_error_breadth(exc: BaseException, expected: Disposition):
    assert so.classify_rest_error(exc) is expected


# ── AC2/AC4: primary-store observation → decide_replay ─────────────────────────


class _RecordingRestClient:
    """Records get_issue_by_rest calls and returns a canned issue (or raises)."""

    def __init__(self, *, summary: str | None = "old", raises: BaseException | None = None) -> None:
        self._summary = summary
        self._raises = raises
        self.calls: list[dict[str, Any]] = []

    def get_issue_by_rest(self, jira_key: str, *, retry_policy: Any = None) -> dict[str, Any]:
        self.calls.append({"jira_key": jira_key, "retry_policy": retry_policy})
        if self._raises is not None:
            raise self._raises
        return {"fields": {"summary": self._summary}}


def test_observe_desired_recovers_with_one_one_attempt_request():
    client = _RecordingRestClient(summary="new title")
    outcome = so.observe_summary_via_rest(client, "PROJ-4", "new title", budget_remaining=True)
    assert outcome.disposition is Disposition.recovered
    assert len(client.calls) == 1, "exactly one primary-store REST request"
    assert client.calls[0]["retry_policy"] is ONE_ATTEMPT_NO_SLEEP
    assert outcome.request_count == 1


def test_observe_old_conclusive_replays_only_within_budget():
    within = so.observe_summary_via_rest(
        _RecordingRestClient(summary="stale"), "PROJ-5", "new", budget_remaining=True
    )
    assert within.disposition is Disposition.retryable_deferred
    exhausted = so.observe_summary_via_rest(
        _RecordingRestClient(summary="stale"), "PROJ-5", "new", budget_remaining=False
    )
    assert exhausted.disposition is Disposition.exhausted_transient


def test_observe_failed_read_is_non_replaying_commit_unknown():
    client = _RecordingRestClient(raises=ConnectionError("reset"))
    outcome = so.observe_summary_via_rest(client, "PROJ-6", "new", budget_remaining=True)
    assert outcome.disposition is Disposition.commit_unknown
    assert outcome.replay_safety is ReplaySafety.forbidden


# ── AC3: the per-call policy is a pass-through; default callers unchanged ───────


def _rest_client() -> acli.AcliClient:
    return acli.AcliClient(
        jira_url="https://example.atlassian.net", user="u", api_token="t", jira_project="DIG"
    )


def test_default_policy_keeps_three_attempts_and_2s_5s_waits(monkeypatch):
    import urllib.request

    import rebar_reconciler.adapters.jira.acli_rest as rest_mod

    slept: list[float] = []
    attempts = {"n": 0}
    monkeypatch.setattr(rest_mod.time, "sleep", lambda s: slept.append(s))

    def boom(_req, timeout=10):
        attempts["n"] += 1
        raise ConnectionError("reset")

    monkeypatch.setattr(rest_mod.urllib.request, "urlopen", boom)
    client = _rest_client()
    req = urllib.request.Request("https://example.atlassian.net/x")
    with pytest.raises(ConnectionError):
        client._rest_urlopen_with_retry(req)  # no policy → legacy default
    assert attempts["n"] == 3
    assert slept == [2, 5]


def test_one_attempt_no_sleep_policy_makes_exactly_one_attempt(monkeypatch):
    import urllib.request

    import rebar_reconciler.adapters.jira.acli_rest as rest_mod

    slept: list[float] = []
    attempts = {"n": 0}
    monkeypatch.setattr(rest_mod.time, "sleep", lambda s: slept.append(s))

    def boom(_req, timeout=10):
        attempts["n"] += 1
        raise ConnectionError("reset")

    monkeypatch.setattr(rest_mod.urllib.request, "urlopen", boom)
    client = _rest_client()
    req = urllib.request.Request("https://example.atlassian.net/x")
    with pytest.raises(ConnectionError):
        client._rest_urlopen_with_retry(req, retry_policy=ONE_ATTEMPT_NO_SLEEP)
    assert attempts["n"] == 1, "one-attempt policy makes exactly one attempt"
    assert slept == [], "one-attempt policy never sleeps"


# ── AC5: historical-triple decoding driven off the sanitized corpus ────────────


def _load_corpus() -> list[dict[str, Any]]:
    return json.loads(_CORPUS.read_text(encoding="utf-8"))["cases"]


@pytest.mark.parametrize("case", _load_corpus(), ids=lambda c: c["name"])
def test_historical_triples_decode_deterministically(case: dict[str, Any]):
    args = (case["returncode"], case["stdout"], case["stderr"])
    if case["expect"] == "unknown":
        with pytest.raises(acli_subprocess.UnknownAcliOutcomeError):
            acli_subprocess.decode_acli_triple(*args)
    else:
        outcome = acli_subprocess.decode_acli_triple(*args)
        assert outcome.value == case["expect"]


def test_corpus_covers_every_known_outcome_and_the_unknown_class():
    expects = {c["expect"] for c in _load_corpus()}
    for outcome in acli_subprocess.AcliOutcome:
        assert outcome.value in expects, f"corpus is missing a case for {outcome}"
    assert "unknown" in expects


# ── AC7: diagnostics are bounded (≤8) and redacted before the 512 cap ──────────


def test_failure_outcome_diagnostic_is_redacted(monkeypatch):
    secret = "sk-ant-deadbeefdeadbeefdeadbeefdeadbeef"

    def fake_run_acli(cmd: list[str], **_kwargs: Any) -> SimpleNamespace:
        raise RuntimeError(secret + " connection reset")

    monkeypatch.setattr(acli_subprocess, "_run_acli", fake_run_acli)
    outcome = so.execute_cloud_summary_write(_fake_client(), "PROJ-7", "s")
    assert outcome.diagnostics, "a failure must carry a diagnostic"
    for entry in outcome.diagnostics:
        assert secret not in entry.get("message", "")
        assert len(entry.get("message", "")) <= 512


def test_diagnostics_are_capped_at_eight_entries():
    from rebar_reconciler.operation_outcome import bound_diagnostics

    raw = [{"stage": "s", "message": f"m{i}"} for i in range(20)]
    bounded = bound_diagnostics(raw)
    assert len(bounded) <= 8


# ── AC8: the completion log — seven fields, bounds, single-object cardinality ───


def _outcome(disposition: Disposition = Disposition.commit_unknown) -> OperationOutcome:
    from rebar_reconciler.operation_outcome import DelaySource, FailureScope

    return OperationOutcome(
        logical_id="11111111-1111-4111-8111-111111111111",
        disposition=disposition,
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
    doc = json.loads(so.render_completion_log(_outcome()))
    assert set(doc) == {
        "logical_id",
        "disposition",
        "invocation_count",
        "request_count",
        "cleanup_status",
        "retry_not_before",
        "message",
    }
    assert doc["disposition"] == Disposition.commit_unknown.value
    assert doc["cleanup_status"] == "not_applicable"
    assert isinstance(doc, dict), "exactly one completion log object, not a list"


def test_completion_log_message_redacted_and_capped_at_512():
    secret = "sk-ant-deadbeefdeadbeefdeadbeefdeadbeef"
    doc = json.loads(so.render_completion_log(_outcome(), message=secret + " " + "." * 900))
    assert secret not in doc["message"]
    assert len(doc["message"]) <= 512


def test_whole_completion_log_capped_at_1024_code_points():
    log = so.render_completion_log(_outcome(), message="." * 5000)
    assert len(log) <= 1024


def test_completion_log_cleanup_status_is_passthrough():
    doc = json.loads(so.render_completion_log(_outcome(), cleanup_status="reaped"))
    assert doc["cleanup_status"] == "reaped"
