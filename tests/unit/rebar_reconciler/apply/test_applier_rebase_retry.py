"""Tests for rebase_retry integration in rebar_reconciler/applier.py.

Covers:
- test_apply_success_attempt1: apply() with rebase_retry succeeding on attempt 1
  returns the manifest path (Result.ok=True path).
- test_apply_success_after_retry: apply() where rebase_retry is monkeypatched to
  simulate a second-attempt success still returns the manifest path.
- test_apply_exhaustion_returns_result: apply() where rebase_retry exhausts all
  attempts returns a Result(ok=False, event.kind='reject_and_reschedule').
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

# ---------------------------------------------------------------------------
# Module loading
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[4]
APPLIER_PATH = (
    REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "applier.py"
)
CONCURRENCY_PATH = (
    REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "_concurrency.py"
)


def _load_applier():
    spec = importlib.util.spec_from_file_location("applier", APPLIER_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["applier"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


def _load_concurrency():
    spec = importlib.util.spec_from_file_location("_concurrency", CONCURRENCY_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("_concurrency", mod)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def applier():
    """Load the applier module, failing all tests if absent."""
    if not APPLIER_PATH.exists():
        pytest.fail(
            f"applier.py not found at {APPLIER_PATH} — "
            "implement the module to make tests pass."
        )
    return _load_applier()


@pytest.fixture(scope="module")
def concurrency():
    """Load the _concurrency module, failing all tests if absent."""
    if not CONCURRENCY_PATH.exists():
        pytest.fail(
            f"_concurrency.py not found at {CONCURRENCY_PATH} — "
            "implement the module to make tests pass."
        )
    return _load_concurrency()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_acli_module() -> tuple[types.ModuleType, MagicMock]:
    """Return (mock acli module, mock client instance) with tracked method calls."""
    mock_client = MagicMock()
    mock_client.create_issue = MagicMock(return_value={"key": "DSO-1"})
    mock_client.update_issue = MagicMock(return_value={"key": "DSO-2"})
    mock_client.transition_issue = MagicMock(return_value=None)
    mock_client.search_issues = MagicMock(return_value=[])

    mock_acli_mod = types.ModuleType("acli_integration")
    mock_acli_mod.AcliClient = MagicMock(return_value=mock_client)

    return mock_acli_mod, mock_client


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_apply_success_attempt1(tmp_path, applier, concurrency):
    """apply() with rebase_retry succeeding on attempt 1 returns the manifest path.

    Verifies that when the tickets-branch write succeeds immediately (HEAD is
    stable, no push rejection), apply() returns the Path to the manifest file.
    """
    pass_id = "2026-05-22-pass-rebase-01"
    mock_acli_mod, _ = _make_mock_acli_module()

    # Build a real Result(ok=True) from the concurrency module
    ok_result = concurrency.Result(ok=True, event=None, value=None)

    # Monkeypatch rebase_retry on the applier module to return ok_result
    original_load_concurrency = applier._load_concurrency

    def fake_load_concurrency():
        fake_mod = types.ModuleType("_concurrency_fake")
        fake_mod.rebase_retry = MagicMock(return_value=ok_result)
        fake_mod.snapshot_head = MagicMock(return_value="aabbccdd" * 5)
        return fake_mod

    applier._load_concurrency = fake_load_concurrency
    try:
        with __import__("unittest.mock", fromlist=["patch"]).patch.object(
            applier, "_load_acli", return_value=mock_acli_mod
        ):
            result = applier.apply([], pass_id, repo_root=tmp_path)
    finally:
        applier._load_concurrency = original_load_concurrency

    expected_path = tmp_path / "bridge_state" / "snapshots" / f"{pass_id}.manifest.json"
    assert result == expected_path, (
        f"Expected manifest path {expected_path}, got {result!r}"
    )
    assert expected_path.exists(), "Manifest file must exist on disk"


def test_apply_success_after_retry(tmp_path, applier, concurrency):
    """apply() where rebase_retry simulates a second-attempt success returns the manifest path.

    The rebase_retry mock is called once and returns ok=True (representing any
    successful attempt, including retries internal to rebase_retry).  apply()
    must propagate that success and return the manifest path.
    """
    pass_id = "2026-05-22-pass-rebase-02"
    mock_acli_mod, _ = _make_mock_acli_module()

    ok_result = concurrency.Result(ok=True, event=None, value=None)

    call_count = {"n": 0}

    def fake_rebase_retry(repo_root, write_fn, *, max_attempts=3):  # noqa: ARG001
        call_count["n"] += 1
        # Always returns success (internal retry logic is inside rebase_retry itself)
        return ok_result

    original_load_concurrency = applier._load_concurrency

    def fake_load_concurrency():
        fake_mod = types.ModuleType("_concurrency_fake")
        fake_mod.rebase_retry = fake_rebase_retry
        fake_mod.snapshot_head = MagicMock(return_value="aabbccdd" * 5)
        return fake_mod

    applier._load_concurrency = fake_load_concurrency
    try:
        with __import__("unittest.mock", fromlist=["patch"]).patch.object(
            applier, "_load_acli", return_value=mock_acli_mod
        ):
            result = applier.apply([], pass_id, repo_root=tmp_path)
    finally:
        applier._load_concurrency = original_load_concurrency

    expected_path = tmp_path / "bridge_state" / "snapshots" / f"{pass_id}.manifest.json"
    assert result == expected_path, (
        f"Expected manifest path after retry, got {result!r}"
    )
    assert call_count["n"] == 1, "rebase_retry must be called exactly once by apply()"


def test_apply_exhaustion_raises_reschedule_error(tmp_path, applier, concurrency):
    """apply() where rebase_retry exhausts all attempts raises RescheduleError.

    When the tickets-branch write cannot succeed within max_attempts, apply()
    raises RescheduleError (task-4 reject-and-reschedule exit path) rather than
    returning a Result object.  The error carries attempt_count and last_error.
    """
    pass_id = "2026-05-22-pass-rebase-03"
    mock_acli_mod, _ = _make_mock_acli_module()

    exhaustion_event = concurrency.ConcurrencyEvent(
        kind="reject_and_reschedule",
        message="exhausted 3 attempts",
        attempt=3,
    )
    exhausted_result = concurrency.Result(ok=False, event=exhaustion_event, value=None)

    original_load_concurrency = applier._load_concurrency

    def fake_load_concurrency():
        fake_mod = types.ModuleType("_concurrency_fake")
        fake_mod.rebase_retry = MagicMock(return_value=exhausted_result)
        fake_mod.snapshot_head = MagicMock(return_value="aabbccdd" * 5)
        return fake_mod

    applier._load_concurrency = fake_load_concurrency
    try:
        with __import__("unittest.mock", fromlist=["patch"]).patch.object(
            applier, "_load_acli", return_value=mock_acli_mod
        ):
            with __import__("pytest").raises(applier.RescheduleError) as exc_info:
                applier.apply([], pass_id, repo_root=tmp_path)
    finally:
        applier._load_concurrency = original_load_concurrency

    err = exc_info.value
    assert err.attempt_count == 3, (
        f"RescheduleError.attempt_count must be 3, got {err.attempt_count}"
    )
    assert "exhausted 3 attempts" in err.last_error, (
        f"RescheduleError.last_error must mention attempt count, got {err.last_error!r}"
    )
