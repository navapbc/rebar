"""Golden additive preview + redaction contract for the sealed versioned outcomes.

REB-3115 S5 T2 — ``manifest_renderer.render_pass_outcomes`` seals a SINGLE additive,
versioned section that exposes every named lifecycle / retry / fuse / outcome field of a
pass without ever touching the canonical mutation array (AC1) and without leaking a secret
or an unbounded collection (AC6).

The section is a PURE projection: identical inputs render byte-identical output, and it is
version-tagged so a legacy reader can ignore it. These tests are the executable spec of
AC2 (every named field present, version-tagged) and AC6 (redact + bound every field and
collection).
"""

from __future__ import annotations

from rebar_reconciler import manifest_renderer
from rebar_reconciler.batch_dispatch import CutoverOutcome
from rebar_reconciler.failure_policy import OUTCOME_BUCKETS
from rebar_reconciler.operation_outcome import (
    DelaySource,
    Disposition,
    FailureScope,
    OperationOutcome,
    ReplaySafety,
    bound_diagnostics,
)
from rebar_reconciler.pass_fuse import FuseDecision


def _outcome(
    logical_id: str,
    disposition: Disposition,
    *,
    failure_scope: FailureScope = FailureScope.none,
    replay_safety: ReplaySafety = ReplaySafety.safe,
    invocation_count: int = 1,
    request_count: int = 1,
    delay_source: DelaySource = DelaySource.none,
    provider_delay_ms: int | None = None,
    retry_not_before: str | None = None,
    diagnostics=(),
) -> OperationOutcome:
    return OperationOutcome(
        logical_id=logical_id,
        disposition=disposition,
        failure_scope=failure_scope,
        replay_safety=replay_safety,
        invocation_count=invocation_count,
        request_count=request_count,
        delay_source=delay_source,
        provider_delay_ms=provider_delay_ms,
        retry_not_before=retry_not_before,
        diagnostics=bound_diagnostics(diagnostics),
    )


# ── AC2 — every named field is present and version-tagged. ───────────────────────


def test_pass_outcomes_section_is_versioned_and_ignorable() -> None:
    section = manifest_renderer.render_pass_outcomes([], tally=None)
    assert isinstance(section["schema_version"], int)
    # A legacy reader can always read the shape: the five canonical buckets are 0-filled
    # and the collections are present-but-empty.
    assert set(section["tally"]) == set(OUTCOME_BUCKETS)
    assert all(section["tally"][bucket] == 0 for bucket in OUTCOME_BUCKETS)
    assert section["outcomes"] == []
    assert section["fuse_state"] == []
    assert section["degraded"] is False


def test_pass_outcomes_entry_carries_every_named_lifecycle_retry_fuse_field() -> None:
    fuse = FuseDecision(
        scope=FailureScope.endpoint.value,
        reason="endpoint_fuse_open",
        retry_not_before="2026-02-01T00:01:00Z",
        provider="jira",
        endpoint="/rest/api/2/issue",
    )
    applied = _outcome(
        "REB-applied",
        Disposition.applied,
        replay_safety=ReplaySafety.safe,
        invocation_count=2,
        request_count=3,
        delay_source=DelaySource.provider,
        provider_delay_ms=1500,
        retry_not_before="2026-02-01T00:00:30Z",
    )
    deferred = CutoverOutcome(
        identity="REB-deferred",
        disposition=Disposition.retryable_deferred,
        bucket="deferred",
        failure_scope=FailureScope.endpoint,
        observation_version=None,
        fuse_decision=fuse,
    )

    section = manifest_renderer.render_pass_outcomes(
        [applied, deferred],
        fuse_decisions=[fuse],
        tally={"applied": 1, "recovered": 0, "deferred": 1, "failed": 0, "skipped": 0},
        degraded=True,
    )

    by_id = {entry["logical_id"]: entry for entry in section["outcomes"]}
    assert by_id.keys() == {"REB-applied", "REB-deferred"}

    a = by_id["REB-applied"]
    # disposition + failure scope + replay safety
    assert a["disposition"] == "applied"
    assert a["failure_scope"] == "none"
    assert a["replay_safety"] == "safe"
    # logical + physical attempts
    assert a["logical_attempts"] == 2
    assert a["physical_attempts"] == 3
    # delay source + value
    assert a["delay_source"] == "provider"
    assert a["delay_value_ms"] == 1500
    # budget envelope (bounds + consumption)
    assert a["budget"]["invocations_used"] == 2
    assert a["budget"]["requests_used"] == 3
    assert isinstance(a["budget"]["max_invocations"], int)
    assert isinstance(a["budget"]["max_cumulative_sleep_ms"], int)
    # retry_not_before
    assert a["retry_not_before"] == "2026-02-01T00:00:30Z"
    # an outcome with no open fuse carries an explicit null fuse
    assert a["fuse"] is None

    d = by_id["REB-deferred"]
    assert d["disposition"] == "retryable_deferred"
    assert d["failure_scope"] == "endpoint"
    # fuse state is attached exactly, with all its named fields
    assert d["fuse"] == {
        "scope": "endpoint",
        "reason": "endpoint_fuse_open",
        "retry_not_before": "2026-02-01T00:01:00Z",
        "provider": "jira",
        "endpoint": "/rest/api/2/issue",
    }

    # pass-level: exact tally, the degraded exit signal, and the distinct fuse state.
    assert section["tally"] == {
        "applied": 1,
        "recovered": 0,
        "deferred": 1,
        "failed": 0,
        "skipped": 0,
    }
    assert section["degraded"] is True
    assert section["fuse_state"] == [
        {
            "scope": "endpoint",
            "reason": "endpoint_fuse_open",
            "retry_not_before": "2026-02-01T00:01:00Z",
            "provider": "jira",
            "endpoint": "/rest/api/2/issue",
        }
    ]


def test_pass_outcomes_is_a_pure_deterministic_projection() -> None:
    outcomes = [
        _outcome("REB-1", Disposition.applied),
        _outcome("REB-2", Disposition.permanent_failure, failure_scope=FailureScope.ticket),
    ]
    first = manifest_renderer.render_pass_outcomes(outcomes, degraded=False)
    second = manifest_renderer.render_pass_outcomes(outcomes, degraded=False)
    assert first == second


# ── AC6 — redact every field and bound every collection. ─────────────────────────


def test_pass_outcomes_redacts_secret_bearing_diagnostics() -> None:
    secret_email = "a" + "dmin@ex" + "ample.com"
    outcome = _outcome(
        "REB-secret",
        Disposition.permanent_failure,
        failure_scope=FailureScope.ticket,
        diagnostics=[{"stage": "apply", "message": f"connect failed for {secret_email}"}],
    )
    section = manifest_renderer.render_pass_outcomes([outcome])
    entry = section["outcomes"][0]
    messages = [d.get("message", "") for d in entry["diagnostics"]]
    joined = " ".join(messages)
    assert secret_email not in joined, "a secret in a diagnostic must be redacted"
    assert "[REDACTED_EMAIL]" in joined


def test_pass_outcomes_bounds_an_unbounded_diagnostic_collection() -> None:
    outcome = _outcome(
        "REB-flood",
        Disposition.permanent_failure,
        failure_scope=FailureScope.ticket,
        diagnostics=[{"stage": "apply", "message": f"attempt {n}"} for n in range(50)],
    )
    section = manifest_renderer.render_pass_outcomes([outcome])
    entry = section["outcomes"][0]
    # bound_diagnostics caps at 8 entries (7 kept + 1 truncation sentinel).
    assert len(entry["diagnostics"]) <= 8
    assert any(d.get("category") == "truncated" for d in entry["diagnostics"])


def test_pass_outcomes_bounds_the_outcomes_collection() -> None:
    flood = [_outcome(f"REB-{n}", Disposition.applied) for n in range(2000)]
    section = manifest_renderer.render_pass_outcomes(flood)
    assert len(section["outcomes"]) <= manifest_renderer._MAX_PASS_OUTCOMES
    # the bound is honest: a truncation sentinel records how many were dropped.
    assert section["outcomes_truncated"] > 0


def test_pass_outcomes_bounds_the_fuse_state_collection() -> None:
    decisions = [
        FuseDecision(
            scope=FailureScope.endpoint.value,
            reason="endpoint_fuse_open",
            retry_not_before="2026-02-01T00:01:00Z",
            provider="jira",
            endpoint=f"/rest/{n}",
        )
        for n in range(2000)
    ]
    section = manifest_renderer.render_pass_outcomes([], fuse_decisions=decisions)
    assert len(section["fuse_state"]) <= manifest_renderer._MAX_PASS_OUTCOMES
