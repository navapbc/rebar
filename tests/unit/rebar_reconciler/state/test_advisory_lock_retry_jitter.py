"""Unit tests for the lock-acquisition retry/jitter band-aid (bug b859-8fa1).

Covers:
  1. Default budget of 5: succeeds on attempt 4 when _rebase_retry fails 3 times.
  2. Custom budget of 2 (via REBAR_RECONCILER_LOCK_RETRY_BUDGET): raises
     ReconcileLockError after 2 failures.
  3. Backoff timing: time.sleep is called between failed attempts, and the
     captured sleep durations satisfy structural backoff invariants (positive,
     bounded by the cap, monotonic non-decreasing base before jitter).
  4. Budget=1 preserves today's fail-fast behaviour (single attempt, no sleep).
  5. Non-drift errors (abort_due_to_error) fail-fast without retry/backoff.

Module loading follows the importlib.util.spec_from_file_location convention
documented in conftest.py.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
LOCK_PATH = REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "_advisory_lock.py"
CONCURRENCY_PATH = REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "_concurrency.py"


def _load_concurrency_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "rebar_reconciler_concurrency_retryj", CONCURRENCY_PATH
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["rebar_reconciler_concurrency_retryj"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _load_advisory_lock_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "rebar_reconciler_advisory_lock_retryj", LOCK_PATH
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["rebar_reconciler_advisory_lock_retryj"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture
def lock_mod():
    return _load_advisory_lock_module()


@pytest.fixture
def conc_mod():
    return _load_concurrency_module()


def _drift_result(conc_mod, attempt: int = 3):
    return conc_mod.Result(
        ok=False,
        event=conc_mod.ConcurrencyEvent(
            kind="reject_and_reschedule",
            message=f"exhausted {attempt} attempts; last drift: HEAD changed aaaa->bbbb",
            attempt=attempt,
        ),
    )


def _ok_result(conc_mod):
    return conc_mod.Result(ok=True, value=None)


def _error_result(conc_mod):
    return conc_mod.Result(
        ok=False,
        event=conc_mod.ConcurrencyEvent(kind="abort_due_to_error", message="boom", attempt=1),
    )


def test_default_budget_succeeds_on_fourth_attempt(lock_mod, conc_mod, monkeypatch):
    """Default budget=5 -> fails 3 times, then succeeds on attempt 4."""
    monkeypatch.delenv("REBAR_RECONCILER_LOCK_RETRY_BUDGET", raising=False)

    call_count = {"n": 0}

    def fake_rebase_retry(repo_root, write_fn, **kwargs):
        call_count["n"] += 1
        if call_count["n"] < 4:
            return _drift_result(conc_mod)
        return _ok_result(conc_mod)

    with patch.object(lock_mod, "_rebase_retry", side_effect=fake_rebase_retry):
        with patch.object(lock_mod.time, "sleep") as sleep_mock:
            lock_mod.acquire_pass_lock("test-pass-id", Path("/fake/repo"))

    assert call_count["n"] == 4, (
        f"Expected 4 _rebase_retry calls (3 fails + 1 success), got {call_count['n']}"
    )
    # 3 sleeps between the 4 attempts
    assert sleep_mock.call_count == 3, (
        f"Expected 3 backoff sleeps between attempts, got {sleep_mock.call_count}"
    )


def test_custom_budget_two_raises_after_two_failures(lock_mod, conc_mod, monkeypatch):
    """Budget=2 -> 2 failures -> raises ReconcileLockError."""
    monkeypatch.setenv("REBAR_RECONCILER_LOCK_RETRY_BUDGET", "2")

    call_count = {"n": 0}

    def always_drift(repo_root, write_fn, **kwargs):
        call_count["n"] += 1
        return _drift_result(conc_mod)

    with patch.object(lock_mod, "_rebase_retry", side_effect=always_drift):
        with patch.object(lock_mod.time, "sleep"):
            with pytest.raises(lock_mod.ReconcileLockError):
                lock_mod.acquire_pass_lock("test-pass-id", Path("/fake/repo"))

    assert call_count["n"] == 2, (
        f"Expected exactly 2 _rebase_retry calls under budget=2, got {call_count['n']}"
    )


def test_backoff_timing_increases_between_retries(lock_mod, conc_mod, monkeypatch):
    """Sleep durations satisfy structural backoff invariants (positive, capped, growing)."""
    monkeypatch.setenv("REBAR_RECONCILER_LOCK_RETRY_BUDGET", "6")

    def always_drift(repo_root, write_fn, **kwargs):
        return _drift_result(conc_mod)

    sleeps: list[float] = []

    def capture_sleep(seconds):
        sleeps.append(seconds)

    with patch.object(lock_mod, "_rebase_retry", side_effect=always_drift):
        with patch.object(lock_mod.time, "sleep", side_effect=capture_sleep):
            with pytest.raises(lock_mod.ReconcileLockError):
                lock_mod.acquire_pass_lock("test-pass-id", Path("/fake/repo"))

    # Assert STRUCTURAL invariants of the backoff schedule, not pinned magic
    # constants (SDET I5). Pinning expected bases [0.2,0.4,0.8,1.6,3.2] broke on
    # any legitimate retune of base/factor/cap even when behavior stayed correct.
    # These invariants still FAIL a genuinely broken backoff (no growth, or
    # unbounded sleeps), while tolerating tuning of the constants:
    #
    #   1. budget=6 -> exactly 5 sleeps (one fewer than the attempt budget).
    #   2. every sleep is strictly positive (a real backoff, never 0/negative).
    #   3. every sleep is bounded by a finite cap (no unbounded growth).
    #   4. the pre-jitter base grows monotonically (non-decreasing) -- exponential
    #      growth that eventually saturates at the cap. We recover each sleep's
    #      pre-jitter base by removing the +/-30% jitter band: base in [s/1.3, s/0.7],
    #      and require successive bases to be non-decreasing allowing for the
    #      jitter overlap (next upper-base >= this lower-base).
    assert len(sleeps) == 5, f"Expected 5 backoff sleeps under budget=6, got {len(sleeps)}"

    JITTER = 0.30  # +/-30%; the only structural fact we rely on about jitter.
    CAP_CEILING = 5.0  # documented hard cap; sleeps may not exceed cap*(1+jitter).

    assert all(s > 0 for s in sleeps), f"every backoff sleep must be > 0; got {sleeps}"
    assert all(s <= CAP_CEILING * (1 + JITTER) for s in sleeps), (
        f"every backoff sleep must be bounded by the cap (<= {CAP_CEILING}s + jitter); got {sleeps}"
    )

    # Recover the jitter-free base band for each sleep, then require the schedule
    # to be monotonic non-decreasing: each step's max possible base must be at
    # least the previous step's min possible base (so a flat/decreasing schedule --
    # i.e. no exponential growth -- fails, while jitter noise does not).
    base_lo = [s / (1 + JITTER) for s in sleeps]
    base_hi = [s / (1 - JITTER) for s in sleeps]
    for i in range(1, len(sleeps)):
        assert base_hi[i] >= base_lo[i - 1], (
            "backoff base must be monotonic non-decreasing (exponential growth, "
            f"saturating at the cap): step {i} base~[{base_lo[i]:.3f},{base_hi[i]:.3f}] "
            f"is below step {i - 1} base~[{base_lo[i - 1]:.3f},{base_hi[i - 1]:.3f}]; "
            f"sleeps={sleeps}"
        )
    # And require *some* real growth across the schedule (not a constant backoff):
    # the last base band must sit strictly above the first (rules out flat sleeps).
    assert base_lo[-1] > base_hi[0], (
        "backoff must actually grow across retries (final base strictly above the "
        f"first); sleeps={sleeps}"
    )


def test_budget_one_is_fail_fast(lock_mod, conc_mod, monkeypatch):
    """Budget=1 -> exactly one attempt, no backoff sleep, fail-fast like today."""
    monkeypatch.setenv("REBAR_RECONCILER_LOCK_RETRY_BUDGET", "1")

    call_count = {"n": 0}

    def always_drift(repo_root, write_fn, **kwargs):
        call_count["n"] += 1
        return _drift_result(conc_mod)

    with patch.object(lock_mod, "_rebase_retry", side_effect=always_drift):
        with patch.object(lock_mod.time, "sleep") as sleep_mock:
            with pytest.raises(lock_mod.ReconcileLockError):
                lock_mod.acquire_pass_lock("test-pass-id", Path("/fake/repo"))

    assert call_count["n"] == 1
    assert sleep_mock.call_count == 0, "Budget=1 must not sleep (fail-fast preservation)"


def test_non_drift_error_fails_fast_without_retry(lock_mod, conc_mod, monkeypatch):
    """abort_due_to_error from _rebase_retry must NOT be retried -- fail fast."""
    monkeypatch.setenv("REBAR_RECONCILER_LOCK_RETRY_BUDGET", "5")

    call_count = {"n": 0}

    def always_error(repo_root, write_fn, **kwargs):
        call_count["n"] += 1
        return _error_result(conc_mod)

    with patch.object(lock_mod, "_rebase_retry", side_effect=always_error):
        with patch.object(lock_mod.time, "sleep") as sleep_mock:
            with pytest.raises(lock_mod.ReconcileLockError):
                lock_mod.acquire_pass_lock("test-pass-id", Path("/fake/repo"))

    assert call_count["n"] == 1, "Non-drift errors must fail fast (single attempt)"
    assert sleep_mock.call_count == 0
