"""Unit tests for the lock-acquisition retry/jitter band-aid (bug b859-8fa1).

Covers:
  1. Default budget of 5: succeeds on attempt 4 when _rebase_retry fails 3 times.
  2. Custom budget of 2 (via DSO_RECONCILER_LOCK_RETRY_BUDGET): raises
     ReconcileLockError after 2 failures.
  3. Backoff timing: time.sleep is called between failed attempts, and the
     captured sleep durations are strictly increasing on average (exponential
     base 200ms x2 per retry, ±30% jitter, capped at 5s).
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

REPO_ROOT = Path(__file__).resolve().parents[3]
LOCK_PATH = (
    REPO_ROOT / "src" / "rebar" / "_engine" / "dso_reconciler" / "_advisory_lock.py"
)
CONCURRENCY_PATH = (
    REPO_ROOT / "src" / "rebar" / "_engine" / "dso_reconciler" / "_concurrency.py"
)


def _load_concurrency_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "dso_reconciler_concurrency_retryj", CONCURRENCY_PATH
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["dso_reconciler_concurrency_retryj"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _load_advisory_lock_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "dso_reconciler_advisory_lock_retryj", LOCK_PATH
    )
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["dso_reconciler_advisory_lock_retryj"] = mod
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
        event=conc_mod.ConcurrencyEvent(
            kind="abort_due_to_error", message="boom", attempt=1
        ),
    )


def test_default_budget_succeeds_on_fourth_attempt(lock_mod, conc_mod, monkeypatch):
    """Default budget=5 → fails 3 times, then succeeds on attempt 4."""
    monkeypatch.delenv("DSO_RECONCILER_LOCK_RETRY_BUDGET", raising=False)

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
    """Budget=2 → 2 failures → raises ReconcileLockError."""
    monkeypatch.setenv("DSO_RECONCILER_LOCK_RETRY_BUDGET", "2")

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
    """Sleep durations are bounded by exponential schedule with ±30% jitter, capped at 5s."""
    monkeypatch.setenv("DSO_RECONCILER_LOCK_RETRY_BUDGET", "6")

    def always_drift(repo_root, write_fn, **kwargs):
        return _drift_result(conc_mod)

    sleeps: list[float] = []

    def capture_sleep(seconds):
        sleeps.append(seconds)

    with patch.object(lock_mod, "_rebase_retry", side_effect=always_drift):
        with patch.object(lock_mod.time, "sleep", side_effect=capture_sleep):
            with pytest.raises(lock_mod.ReconcileLockError):
                lock_mod.acquire_pass_lock("test-pass-id", Path("/fake/repo"))

    # 6 attempts → 5 sleeps
    assert len(sleeps) == 5, f"Expected 5 backoff sleeps, got {len(sleeps)}"

    # Base 200ms, ×2 per retry, capped at 5s, ±30% jitter.
    # Expected bases: 0.2, 0.4, 0.8, 1.6, 3.2 (none capped yet).
    expected_bases = [0.2, 0.4, 0.8, 1.6, 3.2]
    for actual, base in zip(sleeps, expected_bases):
        lo = base * 0.7
        hi = base * 1.3
        # Cap of 5s applies as an upper ceiling regardless of jitter.
        hi = min(hi, 5.0)
        assert lo <= actual <= hi, (
            f"sleep {actual:.3f}s out of expected jitter band [{lo:.3f}, {hi:.3f}] "
            f"for base {base}"
        )


def test_budget_one_is_fail_fast(lock_mod, conc_mod, monkeypatch):
    """Budget=1 → exactly one attempt, no backoff sleep, fail-fast like today."""
    monkeypatch.setenv("DSO_RECONCILER_LOCK_RETRY_BUDGET", "1")

    call_count = {"n": 0}

    def always_drift(repo_root, write_fn, **kwargs):
        call_count["n"] += 1
        return _drift_result(conc_mod)

    with patch.object(lock_mod, "_rebase_retry", side_effect=always_drift):
        with patch.object(lock_mod.time, "sleep") as sleep_mock:
            with pytest.raises(lock_mod.ReconcileLockError):
                lock_mod.acquire_pass_lock("test-pass-id", Path("/fake/repo"))

    assert call_count["n"] == 1
    assert sleep_mock.call_count == 0, (
        "Budget=1 must not sleep (fail-fast preservation)"
    )


def test_non_drift_error_fails_fast_without_retry(lock_mod, conc_mod, monkeypatch):
    """abort_due_to_error from _rebase_retry must NOT be retried — fail fast."""
    monkeypatch.setenv("DSO_RECONCILER_LOCK_RETRY_BUDGET", "5")

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
