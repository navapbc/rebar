"""Tests for HEAD-pin drift detection in applier.apply().

RED task 100b: verifies that:
  1. Drift detected mid-pass aborts after the first mutation (no further Jira calls).
  2. HeadDriftError is raised when HEAD changes mid-pass.
  3. Empty mutation list is a no-op (snapshot_head is NOT invoked).
  4. Stable HEAD (no drift) allows all mutations to be dispatched normally.

All tests mock snapshot_head to return controlled values.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Module loading
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[3]
APPLIER_PATH = (
    REPO_ROOT / "src" / "rebar" / "_engine" / "dso_reconciler" / "applier.py"
)


def _load_applier():
    # Force fresh load so module-level state doesn't bleed between test runs
    spec = importlib.util.spec_from_file_location("applier_drift_test", APPLIER_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["applier_drift_test"] = mod
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


def _make_mock_concurrency_module(head_sequence: list[str]) -> types.ModuleType:
    """Return a mock _concurrency module whose snapshot_head pops from head_sequence.

    Also provides a rebase_retry stub that calls write_fn() without
    invoking snapshot_head (avoids consuming from head_sequence).
    """
    call_iter = iter(head_sequence)

    def _snapshot_head(_repo_root):
        return next(call_iter)

    class _FakeResult:
        ok = True
        event = None
        value = None

    def _fake_rebase_retry(_repo_root, write_fn, **_kwargs):
        write_fn()
        return _FakeResult()

    mock_mod = types.ModuleType("_concurrency_mock")
    mock_mod.snapshot_head = _snapshot_head  # type: ignore[attr-defined]
    mock_mod.rebase_retry = _fake_rebase_retry  # type: ignore[attr-defined]
    return mock_mod


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_drift_mid_pass_raises_head_drift_error(tmp_path, applier):
    """HeadDriftError is raised when HEAD changes between the first and second mutation."""
    # HEAD is stable for the initial pin + first iteration check,
    # then drifts before the second mutation's iteration check.
    sha_stable = "aabbccdd" * 5  # 40 chars
    sha_drifted = "11223344" * 5  # 40 chars

    # Sequence: pin (stable), iter-1 check (stable), iter-2 check (drifted)
    head_seq = [sha_stable, sha_stable, sha_drifted]
    mock_acli_mod, mock_client = _make_mock_acli_module()
    mock_concurrency = _make_mock_concurrency_module(head_seq)

    mutations = [
        {"action": "update", "key": "DSO-10", "fields": {"summary": "first"}},
        {"action": "update", "key": "DSO-11", "fields": {"summary": "second"}},
    ]

    with patch.object(applier, "_load_acli", return_value=mock_acli_mod), \
         patch.object(applier, "_load_concurrency", return_value=mock_concurrency):
        with pytest.raises(applier.HeadDriftError):
            applier.apply(mutations, "pass-drift-01", repo_root=tmp_path)


def test_drift_mid_pass_no_jira_call_after_drift(tmp_path, applier):
    """No Jira call is issued for mutations after the drift is detected."""
    sha_stable = "aabbccdd" * 5
    sha_drifted = "11223344" * 5

    # pin, iter-1 (stable → first mutation runs), iter-2 (drifted → abort)
    head_seq = [sha_stable, sha_stable, sha_drifted]
    mock_acli_mod, mock_client = _make_mock_acli_module()
    mock_concurrency = _make_mock_concurrency_module(head_seq)

    mutations = [
        {"action": "update", "key": "DSO-10", "fields": {"summary": "first"}},
        {"action": "update", "key": "DSO-11", "fields": {"summary": "second"}},
    ]

    with patch.object(applier, "_load_acli", return_value=mock_acli_mod), \
         patch.object(applier, "_load_concurrency", return_value=mock_concurrency):
        with pytest.raises(applier.HeadDriftError):
            applier.apply(mutations, "pass-drift-02", repo_root=tmp_path)

    # Only the first mutation's update_issue should have been called
    # F3: fields unpacked as kwargs (real signature: update_issue(key, **kwargs))
    mock_client.update_issue.assert_called_once_with("DSO-10", summary="first")


def test_empty_mutations_no_head_check(tmp_path, applier):
    """Empty mutation list skips the drift guard: no explicit snapshot_head calls from apply().

    The apply() function must not call snapshot_head for the PIN step or the
    per-iteration check when the mutations list is empty.  (Internal calls made
    by rebase_retry are separately tracked and are not the drift guard.)
    """
    mock_acli_mod, mock_client = _make_mock_acli_module()

    # Tracks calls made directly by apply() (not inside rebase_retry)
    direct_snapshot_calls: list = []

    # Build a mock concurrency module:
    # - snapshot_head tracks calls (used directly by apply() drift guard)
    # - rebase_retry is a no-op stub that returns a successful Result
    mock_concurrency = types.ModuleType("_concurrency_empty")

    class _FakeResult:
        ok = True
        event = None
        value = None

    def _fake_snapshot_head(_repo_root):
        direct_snapshot_calls.append(1)
        return "deadbeefdeadbeefdeadbeefdeadbeefdeadbeef"

    def _fake_rebase_retry(_repo_root, write_fn, **_kwargs):
        # Execute the write function but do NOT call snapshot_head
        write_fn()
        return _FakeResult()

    mock_concurrency.snapshot_head = _fake_snapshot_head  # type: ignore[attr-defined]
    mock_concurrency.rebase_retry = _fake_rebase_retry  # type: ignore[attr-defined]

    with patch.object(applier, "_load_acli", return_value=mock_acli_mod), \
         patch.object(applier, "_load_concurrency", return_value=mock_concurrency):
        manifest_path = applier.apply([], "pass-empty-01", repo_root=tmp_path)

    assert direct_snapshot_calls == [], (
        f"snapshot_head (drift guard) should not be called for empty mutations, "
        f"got {len(direct_snapshot_calls)} calls"
    )
    assert manifest_path.exists(), "Manifest must still be written for empty mutations"


def test_stable_head_all_mutations_dispatched(tmp_path, applier):
    """When HEAD is stable throughout, all mutations are dispatched normally."""
    sha_stable = "aabbccdd" * 5

    # pin + N iteration checks (3 mutations = pin + 3 iteration checks = 4 calls)
    head_seq = [sha_stable] * 4
    mock_acli_mod, mock_client = _make_mock_acli_module()
    mock_concurrency = _make_mock_concurrency_module(head_seq)

    mutations = [
        {"action": "update", "key": "DSO-20", "fields": {"summary": "alpha"}},
        {"action": "update", "key": "DSO-21", "fields": {"summary": "beta"}},
        {"action": "delete", "key": "DSO-22"},
    ]

    with patch.object(applier, "_load_acli", return_value=mock_acli_mod), \
         patch.object(applier, "_load_concurrency", return_value=mock_concurrency):
        manifest_path = applier.apply(mutations, "pass-stable-01", repo_root=tmp_path)

    # All three mutations should have been dispatched
    assert mock_client.update_issue.call_count == 2, (
        f"Expected 2 update_issue calls, got {mock_client.update_issue.call_count}"
    )
    # delete_one now calls client.delete_issue, not transition_issue (which
    # does not exist on AcliClient).
    mock_client.delete_issue.assert_called_once_with("DSO-22")
    assert manifest_path.exists(), "Manifest must be written after successful pass"
