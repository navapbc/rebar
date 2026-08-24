"""[P0] RP-03 S1 T1 — logical-operation outcome + retry-budget contract (happy path).

This file specifies the *correct-behaviour* core of the provider-neutral
logical-operation contract defined in ticket 7bc2-5203-d5f4-4a4a:

- the four enum member sets (:class:`Disposition`, :class:`FailureScope`,
  :class:`ReplaySafety`, :class:`DelaySource`) and their lowercase string values,
- byte-identical canonical serialization through the existing
  ``rebar._store.canonical.canonical_bytes`` seam,
- a well-formed bounded diagnostic,
- the additive-jitter retry schedule (index 0 -> ``[2s,3s)``; index 1 -> ``[4s,5s)``)
  grounded in ADR 0084 / ``acli_subprocess._rate_limit_backoff``,
- a stable ``logical_id`` across physical invocations.

Edge/boundary/ambiguity tables (truncation, overflow sentinel, over-budget
provider delays, exhaustion, observe-before-replay) live in the held-out suite.
Every clock is injected; these tests perform zero wall-clock sleep.
"""

from __future__ import annotations

import pytest

from rebar._store.canonical import canonical_bytes
from rebar_reconciler import retry_budget
from rebar_reconciler.operation_outcome import (
    DelaySource,
    Disposition,
    FailureScope,
    OperationOutcome,
    ReplaySafety,
    bound_diagnostics,
)

# ── Enum member sets (exact) ──────────────────────────────────────────────────


def test_disposition_member_values_are_exact():
    assert {m.value for m in Disposition} == {
        "applied",
        "already_satisfied",
        "recovered",
        "retryable_deferred",
        "commit_unknown",
        "permanent_failure",
        "exhausted_transient",
        "dependency_deferred",
        "scope_deferred",
        "safety_aborted",
        "skipped",
    }


def test_failure_scope_member_values_are_exact():
    assert {m.value for m in FailureScope} == {
        "none",
        "ticket",
        "endpoint",
        "tenant",
        "provider",
        "global",
    }


def test_replay_safety_member_values_are_exact():
    assert {m.value for m in ReplaySafety} == {
        "not_applicable",
        "safe",
        "observe_first",
        "forbidden",
    }


def test_delay_source_member_values_are_exact():
    assert {m.value for m in DelaySource} == {
        "none",
        "fallback_jitter",
        "provider",
        "fuse",
    }


# ── Canonical serialization (byte-identical, non-ASCII preserved) ─────────────


def _outcome(**over):
    base = dict(
        logical_id="11111111-1111-4111-8111-111111111111",
        disposition=Disposition.applied,
        failure_scope=FailureScope.none,
        replay_safety=ReplaySafety.not_applicable,
        invocation_count=1,
        request_count=1,
        delay_source=DelaySource.none,
        provider_delay_ms=None,
        retry_not_before=None,
        diagnostics=(),
    )
    base.update(over)
    return OperationOutcome(**base)


def test_equivalent_outcomes_serialize_byte_identical():
    a = _outcome()
    b = _outcome()
    assert a.canonical_bytes() == b.canonical_bytes()


def test_canonical_bytes_route_through_store_seam_and_keep_non_ascii():
    diag = bound_diagnostics([{"stage": "apply", "category": "note", "message": "café ☕ résumé"}])
    outcome = _outcome(diagnostics=diag)
    produced = outcome.canonical_bytes()
    # Routes through the store seam with its default ascii_only=False: the UTF-8
    # bytes carry the literal multibyte glyphs, never \uXXXX escapes.
    assert produced == canonical_bytes(outcome.to_canonical_dict())
    assert "café ☕ résumé".encode() in produced
    assert b"\\u" not in produced


# ── A well-formed bounded diagnostic ──────────────────────────────────────────


def test_bound_diagnostics_keeps_wellformed_entry():
    out = bound_diagnostics(
        [{"stage": "apply", "category": "transient", "status_code": 503, "message": "unavailable"}]
    )
    assert len(out) == 1
    assert dict(out[0]) == {
        "stage": "apply",
        "category": "transient",
        "status_code": 503,
        "message": "unavailable",
    }


# ── Retry schedule (additive jitter; deterministic endpoints) ─────────────────


def test_retry_index_zero_selects_two_to_three_seconds():
    lo = retry_budget.plan_next_retry(
        retry_index=0,
        invocation_count=1,
        request_count=1,
        cumulative_sleep_ms=0,
        jitter=0.0,
        now_ms=0,
    )
    assert lo.action == "retry"
    assert lo.delay_ms == 2000
    assert lo.delay_source is DelaySource.fallback_jitter


def test_retry_index_one_selects_four_to_five_seconds():
    plan = retry_budget.plan_next_retry(
        retry_index=1,
        invocation_count=2,
        request_count=2,
        cumulative_sleep_ms=2000,
        jitter=0.0,
        now_ms=0,
    )
    assert plan.action == "retry"
    assert plan.delay_ms == 4000
    assert plan.delay_source is DelaySource.fallback_jitter


# ── Stable logical id across physical invocations ─────────────────────────────


def test_logical_id_is_stable_across_invocations():
    lid = "abcdabcd-abcd-4bcd-8bcd-abcdabcdabcd"
    first = _outcome(logical_id=lid, invocation_count=1, request_count=1)
    third = _outcome(logical_id=lid, invocation_count=3, request_count=3)
    assert first.logical_id == third.logical_id == lid


# ── Diagnostic allowlist: only the six admitted keys survive ──────────────────


def test_bound_diagnostics_drops_every_field_outside_the_six_key_allowlist():
    raw = {
        "stage": "apply",
        "category": "transient",
        "status_code": 503,
        "provider_code": "RATE_LIMITED",
        "retry_after_ms": 1200,
        "message": "slow down",
        # everything below must be dropped:
        "authorization": "Bearer abc.def.ghijklmno",
        "request_body": '{"summary": "secret ticket content"}',
        "response_headers": "Set-Cookie: sid=deadbeef",
        "password": "hunter2",
        "ticket_summary": "confidential",
    }
    out = bound_diagnostics([raw])
    assert set(out[0].keys()) == {
        "stage",
        "category",
        "status_code",
        "provider_code",
        "retry_after_ms",
        "message",
    }


def test_absent_optional_keys_are_omitted_not_null():
    out = bound_diagnostics([{"stage": "apply", "category": "note", "message": "ok"}])
    assert "status_code" not in out[0]
    assert "provider_code" not in out[0]
    assert "retry_after_ms" not in out[0]


# ── Redaction: routes message through the centralized ADR-0041 seam ───────────


@pytest.mark.parametrize(
    "secret",
    [
        "leaked sk-ant-ABCDEFGHIJKLMNOP token",
        "Authorization: Bearer abcdef0123456789",
        "contact ops@example.com now",
        "key deadbeefdeadbeefdeadbeefdeadbeef00",
    ],
)
def test_message_is_redacted_through_the_seam(secret):
    from rebar.llm.failure import sanitize_diagnostic

    out = bound_diagnostics([{"stage": "apply", "category": "err", "message": secret}])
    expected = sanitize_diagnostic({"message": secret})["message"]
    assert out[0]["message"] == expected
    # sanity: the raw secret token is gone
    assert "sk-ant-ABCDEFGHIJKLMNOP" not in out[0]["message"]
    assert "ops@example.com" not in out[0]["message"]


# ── 512-code-point truncation (counts code points, not bytes) ─────────────────


def test_message_truncates_at_512_code_points_with_ellipsis():
    # '.' survives the ADR-0041 redactor (not hex/base64/email/key), so this
    # isolates the size bound rather than the redaction that runs before it.
    msg = "." * 600
    out = bound_diagnostics([{"stage": "apply", "category": "note", "message": msg}])
    result = out[0]["message"]
    assert len(result) == 512
    assert result[:511] == "." * 511
    assert result[511] == "…"


def test_message_of_exactly_512_code_points_is_unchanged():
    msg = "." * 512
    out = bound_diagnostics([{"stage": "apply", "category": "note", "message": msg}])
    assert out[0]["message"] == msg


def test_truncation_counts_multibyte_code_points_not_bytes():
    # 600 astral-plane emoji: each is one code point but four UTF-8 bytes.
    msg = "🚀" * 600
    out = bound_diagnostics([{"stage": "apply", "category": "note", "message": msg}])
    result = out[0]["message"]
    assert len(result) == 512
    assert result[:511] == "🚀" * 511
    assert result[511] == "…"


# ── Diagnostic overflow: keep 7 + exact sentinel ──────────────────────────────


def test_eight_or_fewer_entries_pass_through_unchanged():
    entries = [{"stage": "s", "category": "c", "message": str(i)} for i in range(8)]
    out = bound_diagnostics(entries)
    assert len(out) == 8
    assert [d["message"] for d in out] == [str(i) for i in range(8)]


def test_nine_inputs_produce_the_exact_sentinel_example():
    entries = [{"stage": "s", "category": "c", "message": str(i)} for i in range(9)]
    out = bound_diagnostics(entries)
    assert len(out) == 8
    # first seven retained
    assert [d["message"] for d in out[:7]] == [str(i) for i in range(7)]
    # entry eight is EXACTLY the truncation sentinel
    assert dict(out[7]) == {
        "category": "truncated",
        "message": "dropped=2",
        "stage": "diagnostic",
    }


def test_many_inputs_report_correct_dropped_count():
    entries = [{"stage": "s", "category": "c", "message": str(i)} for i in range(20)]
    out = bound_diagnostics(entries)
    assert len(out) == 8
    assert dict(out[7]) == {
        "category": "truncated",
        "message": "dropped=13",  # 20 inputs - 7 retained = 13
        "stage": "diagnostic",
    }


# ── Retry schedule: deterministic jitter endpoints ────────────────────────────


@pytest.mark.parametrize(
    ("retry_index", "jitter", "expected_ms"),
    [
        (0, 0.0, 2000),
        (0, 0.999, 2999),
        (1, 0.0, 4000),
        (1, 0.999, 4999),
    ],
)
def test_jitter_endpoints_stay_within_bounds(retry_index, jitter, expected_ms):
    cum = 0 if retry_index == 0 else 2000
    plan = retry_budget.plan_next_retry(
        retry_index=retry_index,
        invocation_count=retry_index + 1,
        request_count=retry_index + 1,
        cumulative_sleep_ms=cum,
        jitter=jitter,
        now_ms=0,
    )
    assert plan.action == "retry"
    assert plan.delay_ms == expected_ms


# ── Exhaustion: no fourth physical invocation ─────────────────────────────────


def test_no_fourth_invocation_is_permitted():
    plan = retry_budget.plan_next_retry(
        retry_index=2,
        invocation_count=3,
        request_count=3,
        cumulative_sleep_ms=6000,
        jitter=0.0,
        now_ms=0,
    )
    assert plan.action == "exhausted"
    # exhaustion performs no sleep and does not advance the counters
    assert plan.invocation_count == 3
    assert plan.request_count == 3
    assert plan.cumulative_sleep_ms == 6000


# ── Cumulative-sleep ceiling: exact + one-over boundary ───────────────────────


def test_provider_delay_that_exactly_fills_the_budget_still_fits():
    # cumulative 3000 + provider 12000 == 15000 -> fits (<= ceiling)
    plan = retry_budget.plan_next_retry(
        retry_index=1,
        invocation_count=2,
        request_count=2,
        cumulative_sleep_ms=3000,
        jitter=0.0,
        now_ms=0,
        provider_delay_ms=12000,
    )
    assert plan.action == "retry"
    assert plan.delay_ms == 12000
    assert plan.delay_source is DelaySource.provider


def test_provider_delay_one_ms_over_budget_defers_without_sleeping():
    plan = retry_budget.plan_next_retry(
        retry_index=1,
        invocation_count=2,
        request_count=2,
        cumulative_sleep_ms=3001,
        jitter=0.0,
        now_ms=0,
        provider_delay_ms=12000,
    )
    assert plan.action == "defer"
    assert plan.retry_not_before is not None
    # no hidden work: counters unchanged, nothing slept
    assert plan.invocation_count == 2
    assert plan.request_count == 2
    assert plan.cumulative_sleep_ms == 3001


# ── Provider delay is an authoritative LOWER bound, never shortened ───────────


def test_provider_delay_larger_than_jitter_is_used_verbatim():
    plan = retry_budget.plan_next_retry(
        retry_index=0,
        invocation_count=1,
        request_count=1,
        cumulative_sleep_ms=0,
        jitter=0.0,
        now_ms=0,
        provider_delay_ms=2500,  # > the 2000 ms jitter floor for index 0
    )
    assert plan.delay_ms == 2500
    assert plan.delay_source is DelaySource.provider


def test_provider_delay_smaller_than_jitter_does_not_shorten_the_sleep():
    plan = retry_budget.plan_next_retry(
        retry_index=1,
        invocation_count=2,
        request_count=2,
        cumulative_sleep_ms=2000,
        jitter=0.0,
        now_ms=0,
        provider_delay_ms=100,  # < the 4000 ms jitter floor for index 1
    )
    assert plan.delay_ms == 4000
    assert plan.delay_source is DelaySource.fallback_jitter


# ── retry_not_before is UTC RFC 3339 with a trailing Z ────────────────────────


def test_retry_not_before_is_rfc3339_utc_with_z():
    plan = retry_budget.plan_next_retry(
        retry_index=1,
        invocation_count=2,
        request_count=2,
        cumulative_sleep_ms=14000,
        jitter=0.0,
        now_ms=1_700_000_000_000,
        provider_delay_ms=12000,
    )
    assert plan.action == "defer"
    assert plan.retry_not_before.endswith("Z")
    # parseable as an RFC 3339 timestamp
    from datetime import datetime

    datetime.fromisoformat(plan.retry_not_before.replace("Z", "+00:00"))


# ── Zero wall-clock: fake clocks prove no real time passes ────────────────────


class _PoisonClock:
    """Advances virtual time on read; FAILS LOUDLY if anything sleeps."""

    def __init__(self, start_ms=0):
        self.now_ms = start_ms

    def now(self):
        return self.now_ms

    def sleep_ms(self, ms):  # must never fire on a defer/exhaust path
        raise AssertionError(f"unexpected sleep of {ms} ms")


class _CountingClock:
    """Records requested sleeps and advances virtual time — never really sleeps."""

    def __init__(self, start_ms=0):
        self.now_ms = start_ms
        self.slept = []

    def now(self):
        return self.now_ms

    def sleep_ms(self, ms):
        self.slept.append(ms)
        self.now_ms += ms


def test_deferred_retry_performs_no_sleep_via_poison_clock():
    budget = retry_budget.RetryBudget(clock=_PoisonClock(), jitter=lambda: 0.0)
    # An over-budget provider delay on the first retry must defer, not sleep.
    plan = budget.attempt_retry(provider_delay_ms=999_999)
    assert plan.action == "defer"
    assert plan.retry_not_before is not None
    assert budget.invocation_count == 1  # still only the initial invocation


def test_full_schedule_completes_with_zero_wall_clock_and_then_exhausts():
    clock = _CountingClock()
    budget = retry_budget.RetryBudget(clock=clock, jitter=lambda: 0.0)
    first = budget.attempt_retry()
    second = budget.attempt_retry()
    third = budget.attempt_retry()
    assert first.action == "retry" and first.delay_ms == 2000
    assert second.action == "retry" and second.delay_ms == 4000
    assert third.action == "exhausted"
    # exactly two fake sleeps happened; the third attempt slept nothing
    assert clock.slept == [2000, 4000]
    assert budget.invocation_count == 3


# ── Observe-before-replay ambiguity table ─────────────────────────────────────


def test_observed_desired_state_returns_recovered_without_replay():
    disposition, replay = retry_budget.decide_replay(observation="desired", budget_remaining=True)
    assert disposition is Disposition.recovered
    assert replay is False


def test_conclusive_old_state_replays_only_within_remaining_budget():
    disp_ok, replay_ok = retry_budget.decide_replay(
        observation="old_conclusive", budget_remaining=True
    )
    assert replay_ok is True
    assert disp_ok is Disposition.retryable_deferred

    disp_exhausted, replay_exhausted = retry_budget.decide_replay(
        observation="old_conclusive", budget_remaining=False
    )
    assert replay_exhausted is False
    assert disp_exhausted is Disposition.exhausted_transient


@pytest.mark.parametrize("observation", ["failed", "inconclusive"])
def test_failed_or_inconclusive_observation_forbids_replay(observation):
    disposition, replay = retry_budget.decide_replay(observation=observation, budget_remaining=True)
    assert disposition is Disposition.commit_unknown
    assert replay is False


def test_commit_unknown_carries_replay_safety_forbidden():
    safety = retry_budget.replay_safety_for(Disposition.commit_unknown)
    assert safety is ReplaySafety.forbidden


# ── Defense-in-depth redaction: non-string messages and non-message keys ──────


def test_non_string_message_whose_repr_carries_a_secret_is_redacted():
    # A dict/exception message is coerced to text *before* the seam so a secret in its
    # repr cannot bypass redaction (it must not reach the canonical bytes verbatim).
    secret = "sk-ant-deadbeefdeadbeefdeadbeef"
    out = bound_diagnostics([{"stage": "apply", "category": "err", "message": {"token": secret}}])
    rendered = dict(out[0])["message"]
    assert secret not in rendered
    assert "[REDACTED_KEY]" in rendered


def test_non_message_string_keys_are_also_redacted_through_the_seam():
    # provider_code/stage/category are copied into canonical bytes; a secret smuggled into
    # one must not survive verbatim.
    secret = "sk-ant-cafebabecafebabecafebabe"
    out = bound_diagnostics(
        [{"stage": "apply", "category": "err", "provider_code": secret, "message": "boom"}]
    )
    assert secret not in dict(out[0])["provider_code"]
    assert secret.encode() not in _outcome(diagnostics=out).canonical_bytes()


def test_integer_diagnostic_values_pass_through_unchanged():
    out = bound_diagnostics([{"stage": "apply", "category": "note", "status_code": 503}])
    assert dict(out[0])["status_code"] == 503


# ── OperationOutcome optional-field serialization (both branches) ──────────────


def test_optional_outcome_fields_are_serialized_only_when_present():
    filled = _outcome(
        disposition=Disposition.retryable_deferred,
        delay_source=DelaySource.provider,
        provider_delay_ms=4200,
        retry_not_before="2026-01-01T00:00:00Z",
    ).to_canonical_dict()
    assert filled["provider_delay_ms"] == 4200
    assert filled["retry_not_before"] == "2026-01-01T00:00:00Z"

    empty = _outcome().to_canonical_dict()
    assert "provider_delay_ms" not in empty
    assert "retry_not_before" not in empty


# ── decide_replay / replay_safety_for total-function guards ────────────────────


def test_unrecognized_observation_fails_loud():
    with pytest.raises(ValueError, match="unrecognized observation"):
        retry_budget.decide_replay(observation="typo_state", budget_remaining=True)


@pytest.mark.parametrize(
    "disposition",
    [Disposition.applied, Disposition.recovered, Disposition.retryable_deferred],
)
def test_non_commit_unknown_dispositions_carry_replay_safety_not_applicable(disposition):
    assert retry_budget.replay_safety_for(disposition) is ReplaySafety.not_applicable
