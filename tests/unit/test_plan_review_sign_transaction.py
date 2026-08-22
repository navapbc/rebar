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


def _transaction(monkeypatch, fresh=None, heads=None, signer=None):
    initial = _generation()
    head_values = iter(heads) if heads is not None else None
    monkeypatch.setattr(generation.config, "tracker_dir", lambda root=None: "/tracker")
    monkeypatch.setattr(
        generation,
        "tracker_head_sha",
        lambda *a, **k: next(head_values) if head_values is not None else "b" * 40,
    )
    monkeypatch.setattr(generation, "collect", lambda *a, **k: fresh or initial)
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


def test_head_instability_retries_exactly_three_times(monkeypatch, caplog) -> None:
    initial = _transaction(
        monkeypatch,
        heads=[value * 40 for value in ("a", "b", "c", "d", "e", "f")],
    )
    with pytest.raises(generation.PlanReviewGenerationRetryable):
        generation.sign_manifest("1111-2222-3333-4444", ["plan-review: PASS"], initial)
    retries = [
        r for r in caplog.records if getattr(r, "event", None) == "plan_review_generation_retry"
    ]
    assert [(r.attempt, r.before, r.after) for r in retries] == [
        (1, "a" * 40, "b" * 40),
        (2, "c" * 40, "d" * 40),
        (3, "e" * 40, "f" * 40),
    ]


def test_lock_timeout_is_retryable_and_never_writes(monkeypatch) -> None:
    from rebar._store.lock import LockTimeout

    initial = _transaction(
        monkeypatch,
        signer=lambda *a, **k: (_ for _ in ()).throw(LockTimeout(1)),
    )
    with pytest.raises(generation.PlanReviewGenerationRetryable):
        generation.sign_manifest("1111-2222-3333-4444", ["plan-review: PASS"], initial)


def test_under_lock_mismatch_retries_then_returns_retryable(monkeypatch, caplog) -> None:
    initial = _transaction(
        monkeypatch,
        heads=[value * 40 for value in ("a", "a", "b", "c", "c", "d", "e", "e", "f")],
    )
    with pytest.raises(generation.PlanReviewGenerationRetryable):
        generation.sign_manifest("1111-2222-3333-4444", ["plan-review: PASS"], initial)
    retries = [
        r for r in caplog.records if getattr(r, "event", None) == "plan_review_generation_retry"
    ]
    assert [r.after for r in retries] == ["under-lock-mismatch"] * 3


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


def _raising_head(reason: str, fail_times: int, stable: str = "b" * 40):
    """tracker_head_sha stub: raise store-read-failure the first ``fail_times`` calls."""
    from rebar.llm.plan_review.relation_snapshot import PlanRelationSnapshotError

    calls = {"n": 0}

    def _head(*a, **k):
        calls["n"] += 1
        if calls["n"] <= fail_times:
            raise PlanRelationSnapshotError(reason)
        return stable

    return _head


def test_transient_store_read_failure_is_retried_then_signs(monkeypatch, caplog) -> None:
    """A peer session's in-flight staged index (transient store-read-failure) must be
    retried like a moving HEAD, not aborted terminally (bug ec1e / staged-index race)."""
    initial = _transaction(monkeypatch)
    monkeypatch.setattr(
        generation, "tracker_head_sha", _raising_head("store-read-failure", fail_times=1)
    )
    assert generation.sign_manifest("1111-2222-3333-4444", ["plan-review: PASS"], initial) == {
        "signed": True
    }


def test_persistent_store_read_failure_is_retryable_not_terminal(monkeypatch) -> None:
    """A store-read-failure that never clears exhausts the retry budget and surfaces as
    a RETRYABLE signal, not a terminal unsigned PlanReviewGenerationError."""
    initial = _transaction(monkeypatch)
    monkeypatch.setattr(
        generation, "tracker_head_sha", _raising_head("store-read-failure", fail_times=99)
    )
    with pytest.raises(generation.PlanReviewGenerationRetryable):
        generation.sign_manifest("1111-2222-3333-4444", ["plan-review: PASS"], initial)
