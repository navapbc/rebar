"""Production-path regression for sidecar/sign generation ordering."""

from __future__ import annotations

from dataclasses import replace

import pytest

from rebar.llm.plan_review import generation
from rebar.llm.plan_review.relation_snapshot import PlanRelationSnapshot


def _generation() -> generation.PlanReviewGeneration:
    snapshot = PlanRelationSnapshot(
        subject_state={"ticket_id": "1111-2222-3333-4444", "status": "in_progress"},
        ticket_states_by_id={},
        child_ids=(),
        prerequisite_ids=(),
        related_material=(),
        ticket_store_revision="a" * 40,
    )
    return generation.PlanReviewGeneration(
        phase="execution",
        priority_floor=0.8,
        own_material="1111111111111111",
        relation_snapshot=snapshot,
        ticket_store_revision="a" * 40,
    )


def _transaction(monkeypatch, fresh=None, collect=None, signer=None):
    initial = _generation()
    monkeypatch.setattr(generation, "collect", collect or (lambda *a, **k: fresh or initial))
    if signer is None:

        def signer(ticket_id, manifest, **kwargs):
            kwargs["under_lock_check"]()
            return {"signed": True}

    monkeypatch.setattr("rebar.signing._sign_manifest_under_lock", signer)
    return initial


def test_stable_generation_rechecks_under_lock_and_signs(monkeypatch) -> None:
    initial = _transaction(monkeypatch)
    assert generation.sign_manifest("1111-2222-3333-4444", ["plan-review: PASS"], initial) == {
        "signed": True
    }


def test_stable_generation_change_is_terminal_not_retried(monkeypatch) -> None:
    initial = _generation()
    changed = replace(initial, own_material="2222222222222222")
    _transaction(monkeypatch, fresh=changed)
    with pytest.raises(generation.PlanReviewGenerationChanged):
        generation.sign_manifest("1111-2222-3333-4444", ["plan-review: PASS"], initial)


def _parity_collect(initial, drifted):
    """collect stub: the pre-lock read (odd calls) sees ``initial``, the under-lock
    re-read (even calls) sees ``drifted`` — a flapping generation."""
    calls = {"n": 0}

    def _collect(*a, **k):
        calls["n"] += 1
        return initial if calls["n"] % 2 else drifted

    return _collect


def test_under_lock_generation_drift_retries_named_then_exhausts(monkeypatch, caplog) -> None:
    """Bug a83f AC-5: an under-lock mismatch can only be GENERATION drift now (the
    store-wide ``locked_head`` clause is gone), and each retry names it — a bare
    ``after="under-lock-mismatch"`` gave 25 field retries no actionable signal."""
    initial = _generation()
    drifted = replace(initial, own_material="2222222222222222")
    _transaction(monkeypatch, collect=_parity_collect(initial, drifted))
    with pytest.raises(generation.PlanReviewGenerationRetryable):
        generation.sign_manifest("1111-2222-3333-4444", ["plan-review: PASS"], initial)
    retries = [
        r for r in caplog.records if getattr(r, "event", None) == "plan_review_generation_retry"
    ]
    assert [(r.attempt, r.reason) for r in retries] == [
        (1, "generation-drift-under-lock"),
        (2, "generation-drift-under-lock"),
        (3, "generation-drift-under-lock"),
    ]


def test_under_lock_generation_drift_is_named_terminally_next_attempt(monkeypatch) -> None:
    """Persistent drift caught under lock fails closed on the NEXT attempt's pre-lock
    comparison, which names the changed component (PlanReviewGenerationChanged)."""
    initial = _generation()
    drifted = replace(initial, own_material="2222222222222222")
    calls = {"n": 0}

    def _collect(*a, **k):
        calls["n"] += 1
        return initial if calls["n"] == 1 else drifted

    _transaction(monkeypatch, collect=_collect)
    with pytest.raises(generation.PlanReviewGenerationChanged):
        generation.sign_manifest("1111-2222-3333-4444", ["plan-review: PASS"], initial)


def test_lock_timeout_is_retryable_and_never_writes(monkeypatch) -> None:
    from rebar._store.lock import LockTimeout

    initial = _transaction(
        monkeypatch,
        signer=lambda *a, **k: (_ for _ in ()).throw(LockTimeout(1)),
    )
    with pytest.raises(generation.PlanReviewGenerationRetryable):
        generation.sign_manifest("1111-2222-3333-4444", ["plan-review: PASS"], initial)


def test_terminal_sign_failure_is_structured_and_unsigned(monkeypatch, caplog) -> None:
    initial = _transaction(
        monkeypatch,
        signer=lambda *a, **k: (_ for _ in ()).throw(RuntimeError("sign failed")),
    )
    with pytest.raises(generation.PlanReviewGenerationError):
        generation.sign_manifest("1111-2222-3333-4444", ["plan-review: PASS"], initial)
    error = next(
        r for r in caplog.records if getattr(r, "event", None) == "plan_review_sign_aborted"
    )
    assert (error.attempt, error.reason) == (1, "RuntimeError")


def _raising_collect(reason: str, fail_times: int, result=None):
    """generation.collect stub: raise ``reason`` the first ``fail_times`` calls."""
    from rebar.llm.plan_review.relation_snapshot import PlanRelationSnapshotError

    calls = {"n": 0}

    def _collect(*a, **k):
        calls["n"] += 1
        if calls["n"] <= fail_times:
            raise PlanRelationSnapshotError(reason)
        return result

    return _collect


def test_transient_store_read_failure_is_retried_then_signs(monkeypatch, caplog) -> None:
    """A peer session's in-flight staged index (transient store-read-failure) must be
    retried, not aborted terminally (bug ec1e / staged-index race)."""
    initial = _generation()
    _transaction(monkeypatch, collect=_raising_collect("store-read-failure", 1, result=initial))
    assert generation.sign_manifest("1111-2222-3333-4444", ["plan-review: PASS"], initial) == {
        "signed": True
    }


def test_persistent_store_read_failure_is_retryable_not_terminal(monkeypatch) -> None:
    """A store-read-failure that never clears exhausts the retry budget and surfaces as
    a RETRYABLE signal, not a terminal unsigned PlanReviewGenerationError."""
    initial = _generation()
    _transaction(monkeypatch, collect=_raising_collect("store-read-failure", 99))
    with pytest.raises(generation.PlanReviewGenerationRetryable):
        generation.sign_manifest("1111-2222-3333-4444", ["plan-review: PASS"], initial)


def test_deterministic_relation_reason_stays_terminal_not_retried(monkeypatch, caplog) -> None:
    """Only the transient ``store-read-failure`` is retried; a deterministic reason
    (e.g. a malformed reference) must still abort terminally on the FIRST attempt."""
    initial = _generation()
    _transaction(monkeypatch, collect=_raising_collect("malformed-reference", 99))
    with pytest.raises(generation.PlanReviewGenerationError) as exc_info:
        generation.sign_manifest("1111-2222-3333-4444", ["plan-review: PASS"], initial)
    assert not isinstance(exc_info.value, generation.PlanReviewGenerationRetryable)
    aborted = [r for r in caplog.records if getattr(r, "event", None) == "plan_review_sign_aborted"]
    assert [(r.attempt, r.reason) for r in aborted] == [(1, "malformed-reference")]


def _under_lock_raising_collect(initial, reason: str, fail_times: int):
    """collect stub: pre-lock reads (odd calls) return ``initial``; the under-lock
    re-read (even calls) raises ``reason`` for the first ``fail_times`` attempts."""
    from rebar.llm.plan_review.relation_snapshot import PlanRelationSnapshotError

    calls = {"n": 0, "failed": 0}

    def _collect(*a, **k):
        calls["n"] += 1
        if calls["n"] % 2 == 0 and calls["failed"] < fail_times:
            calls["failed"] += 1
            raise PlanRelationSnapshotError(reason)
        return initial

    return _collect


def test_under_lock_store_read_failure_is_retried_then_signs(monkeypatch, caplog) -> None:
    """A transient store-read-failure raised by the under-lock re-collect is the SAME
    peer-writer race as the pre-lock one and must take the same bounded retry path —
    not fall through to the generic terminal handler as an unsigned
    PlanReviewGenerationError (LLM-Review finding on change 2142)."""
    initial = _generation()
    _transaction(monkeypatch, collect=_under_lock_raising_collect(initial, "store-read-failure", 1))
    assert generation.sign_manifest("1111-2222-3333-4444", ["plan-review: PASS"], initial) == {
        "signed": True
    }
    retries = [
        r for r in caplog.records if getattr(r, "event", None) == "plan_review_generation_retry"
    ]
    assert [(r.attempt, r.reason) for r in retries] == [(1, "store-read-failure")]


def test_under_lock_persistent_store_read_failure_is_retryable(monkeypatch) -> None:
    """An under-lock store-read-failure that never clears exhausts the attempt budget
    as RETRYABLE, exactly like the pre-lock variant."""
    initial = _generation()
    _transaction(
        monkeypatch, collect=_under_lock_raising_collect(initial, "store-read-failure", 99)
    )
    with pytest.raises(generation.PlanReviewGenerationRetryable):
        generation.sign_manifest("1111-2222-3333-4444", ["plan-review: PASS"], initial)


def test_under_lock_deterministic_reason_stays_terminal(monkeypatch, caplog) -> None:
    """A deterministic snapshot reason under lock (e.g. malformed reference) aborts
    terminally with the snapshot's own reason, never retried."""
    initial = _generation()
    _transaction(
        monkeypatch, collect=_under_lock_raising_collect(initial, "malformed-reference", 99)
    )
    with pytest.raises(generation.PlanReviewGenerationError) as exc_info:
        generation.sign_manifest("1111-2222-3333-4444", ["plan-review: PASS"], initial)
    assert not isinstance(exc_info.value, generation.PlanReviewGenerationRetryable)
    aborted = [r for r in caplog.records if getattr(r, "event", None) == "plan_review_sign_aborted"]
    assert [(r.attempt, r.reason) for r in aborted] == [(1, "malformed-reference")]
