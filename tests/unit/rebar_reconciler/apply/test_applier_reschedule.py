"""Tests for the reject-and-reschedule exit path in rebar_reconciler/applier.py.

RED task 689c: verifies that:
  1. When rebase_retry returns Result(ok=False, event.kind='reject_and_reschedule'),
     apply() raises RescheduleError (not returning a plain Result).
  2. The health event JSON {kind, pass_id, attempt_count, last_error} is emitted
     to stderr before the raise.
  3. No retry-counter file is written to disk after exhaustion.
  4. Pass N+1 (with contention removed) succeeds with no residual state from
     pass N — the next pass starts fresh.

All tests mock rebase_retry (via monkeypatching _load_concurrency) so the
tickets-branch git operations are never executed.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Module loading
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[4]
APPLIER_PATH = REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "applier.py"
CONCURRENCY_PATH = REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "_concurrency.py"


def _load_applier():
    """Load applier module with a fresh module name to avoid cache collisions."""
    spec = importlib.util.spec_from_file_location("applier_reschedule_test", APPLIER_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["applier_reschedule_test"] = mod
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
            f"applier.py not found at {APPLIER_PATH} — implement the module to make tests pass."
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


def _make_exhaustion_concurrency(concurrency_mod) -> types.ModuleType:
    """Return a fake _concurrency module whose rebase_retry always returns exhaustion."""
    exhaustion_event = concurrency_mod.ConcurrencyEvent(
        kind="reject_and_reschedule",
        message="exhausted 3 attempts",
        attempt=3,
    )
    exhausted_result = concurrency_mod.Result(ok=False, event=exhaustion_event, value=None)

    fake_mod = types.ModuleType("_concurrency_exhaustion")
    fake_mod.rebase_retry = MagicMock(return_value=exhausted_result)
    fake_mod.snapshot_head = MagicMock(return_value="aabbccdd" * 5)
    return fake_mod


def _make_success_concurrency(concurrency_mod) -> types.ModuleType:
    """Return a fake _concurrency module whose rebase_retry always returns success."""
    ok_result = concurrency_mod.Result(ok=True, event=None, value=None)

    fake_mod = types.ModuleType("_concurrency_success")
    fake_mod.rebase_retry = MagicMock(return_value=ok_result)
    fake_mod.snapshot_head = MagicMock(return_value="aabbccdd" * 5)
    return fake_mod


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_apply_raises_reschedule_error_on_exhaustion(tmp_path, applier, concurrency):
    """apply() raises RescheduleError when rebase_retry returns reject_and_reschedule.

    This is the primary exit path for task 689c.  When the tickets-branch write
    cannot succeed within max_attempts, apply() must raise RescheduleError rather
    than returning the failed Result object.
    """
    pass_id = "2026-05-22-pass-reschedule-01"
    mock_acli_mod, _ = _make_mock_acli_module()
    fake_concurrency = _make_exhaustion_concurrency(concurrency)

    with (
        patch.object(applier, "_load_acli", return_value=mock_acli_mod),
        patch.object(applier, "_load_concurrency", return_value=fake_concurrency),
    ):
        with pytest.raises(applier.RescheduleError) as exc_info:
            applier.apply([], pass_id, repo_root=tmp_path)

    err = exc_info.value
    assert err.attempt_count == 3, (
        f"RescheduleError.attempt_count should be 3, got {err.attempt_count}"
    )
    assert "exhausted 3 attempts" in err.last_error, (
        f"RescheduleError.last_error should mention attempt count, got {err.last_error!r}"
    )


def test_apply_emits_health_event_to_stderr_on_exhaustion(tmp_path, applier, concurrency, capsys):
    """apply() emits a JSON health event to stderr before raising RescheduleError.

    The event must include: kind='reject_and_reschedule', attempt_count, last_error.
    """
    pass_id = "2026-05-22-pass-reschedule-02"
    mock_acli_mod, _ = _make_mock_acli_module()
    fake_concurrency = _make_exhaustion_concurrency(concurrency)

    with (
        patch.object(applier, "_load_acli", return_value=mock_acli_mod),
        patch.object(applier, "_load_concurrency", return_value=fake_concurrency),
    ):
        with pytest.raises(applier.RescheduleError):
            applier.apply([], pass_id, repo_root=tmp_path)

    captured = capsys.readouterr()
    stderr_lines = [line for line in captured.err.strip().splitlines() if line.strip()]
    assert stderr_lines, "apply() must emit at least one line to stderr on exhaustion"

    # The last line emitted should be the reschedule health event
    health_event = json.loads(stderr_lines[-1])
    assert health_event.get("kind") == "reject_and_reschedule", (
        f"Health event kind must be 'reject_and_reschedule', got {health_event.get('kind')!r}"
    )
    assert "attempt_count" in health_event, "Health event must include 'attempt_count'"
    assert "last_error" in health_event, "Health event must include 'last_error'"
    assert health_event.get("pass_id") == pass_id, (
        f"Health event pass_id must match, got {health_event.get('pass_id')!r}"
    )


def test_no_retry_counter_file_written_after_exhaustion(tmp_path, applier, concurrency):
    """No retry-counter file is written to disk after rebase_retry exhaustion.

    The 'no partial state' guarantee: a failed write leaves the manifest on disk
    but no counter file.  This ensures pass N+1 starts completely fresh.
    """
    pass_id = "2026-05-22-pass-reschedule-03"
    mock_acli_mod, _ = _make_mock_acli_module()
    fake_concurrency = _make_exhaustion_concurrency(concurrency)

    with (
        patch.object(applier, "_load_acli", return_value=mock_acli_mod),
        patch.object(applier, "_load_concurrency", return_value=fake_concurrency),
    ):
        with pytest.raises(applier.RescheduleError):
            applier.apply([], pass_id, repo_root=tmp_path)

    # Collect all files written under tmp_path
    all_files = list(tmp_path.rglob("*"))
    counter_files = [
        f
        for f in all_files
        if f.is_file() and "retry" in f.name.lower() and "counter" in f.name.lower()
    ]
    assert counter_files == [], (
        f"No retry-counter file should exist after exhaustion, found: {counter_files}"
    )

    # Also assert no file with 'retry_count' or similar suffix anywhere
    retry_state_files = [
        f
        for f in all_files
        if f.is_file()
        and any(tok in f.name.lower() for tok in ("retry_count", "retry-count", "attempt_count"))
    ]
    assert retry_state_files == [], (
        f"No retry-state file should persist across passes, found: {retry_state_files}"
    )


def test_pass_n_plus_1_succeeds_with_no_residual_state(tmp_path, applier, concurrency):
    """Pass N+1 succeeds after a failed pass N, with no residual state carrying over.

    Simulates the full reject-and-reschedule -> fresh-pass cycle:
      - Pass N: rebase_retry exhausted → RescheduleError raised.
      - Pass N+1: rebase_retry succeeds → manifest path returned.

    The manifest for pass N+1 must exist on disk and the manifest for pass N
    must also exist (written before the write attempt, for idempotency), but
    pass N+1 must succeed independently without any residual state from pass N.
    """
    pass_n_id = "2026-05-22-pass-N"
    pass_n1_id = "2026-05-22-pass-N1"
    mock_acli_mod, _ = _make_mock_acli_module()

    # ---- Pass N: exhaustion scenario ----
    fake_exhaustion = _make_exhaustion_concurrency(concurrency)
    with (
        patch.object(applier, "_load_acli", return_value=mock_acli_mod),
        patch.object(applier, "_load_concurrency", return_value=fake_exhaustion),
    ):
        with pytest.raises(applier.RescheduleError):
            applier.apply([], pass_n_id, repo_root=tmp_path)

    # The manifest for pass N should have been written (idempotency guarantee)
    manifest_n = tmp_path / "bridge_state" / "snapshots" / f"{pass_n_id}.manifest.json"
    assert manifest_n.exists(), (
        f"Pass N manifest must be on disk after exhaustion (idempotency), path: {manifest_n}"
    )

    # ---- Pass N+1: success scenario (contention removed) ----
    fake_success = _make_success_concurrency(concurrency)
    with (
        patch.object(applier, "_load_acli", return_value=mock_acli_mod),
        patch.object(applier, "_load_concurrency", return_value=fake_success),
    ):
        result_path = applier.apply([], pass_n1_id, repo_root=tmp_path)

    manifest_n1 = tmp_path / "bridge_state" / "snapshots" / f"{pass_n1_id}.manifest.json"
    assert result_path == manifest_n1, (
        f"Pass N+1 must return the manifest path, got {result_path!r}"
    )
    assert manifest_n1.exists(), "Pass N+1 manifest must exist on disk"

    # Verify pass N+1 manifest is independent (no contamination from pass N)
    n1_data = json.loads(manifest_n1.read_text())
    assert n1_data.get("pass_id") == pass_n1_id, (
        f"Pass N+1 manifest pass_id must be {pass_n1_id!r}, got {n1_data.get('pass_id')!r}"
    )


def test_run_pass_returns_exit_reschedule_when_reconcile_signals_reschedule(
    tmp_path, monkeypatch, applier
):
    """The reschedule EXIT CODE is asserted by its EFFECT, not its literal value.

    Drive the real ``run_pass`` orchestrator with a reconcile whose
    ``reconcile_once`` raises the applier's ``RescheduleError`` (the exhaustion
    signal). ``run_pass`` classifies that and RETURNS its reschedule exit code —
    we assert the returned code IS ``applier.EXIT_RESCHEDULE`` and is distinct
    from the success (0) and generic-error (1) codes that share the same seam.
    """
    import rebar_reconciler.__main__ as main_mod

    # A fake reconcile module whose reconcile_once raises the REAL RescheduleError
    # (same class object run_pass will isinstance-check, since we hand run_pass the
    # same applier fixture below).
    fake_reconcile = types.ModuleType("reconcile_reschedule_fake")

    def _raise_reschedule(*_args, **_kwargs):
        raise applier.RescheduleError(attempt_count=3, last_error="exhausted 3 attempts")

    fake_reconcile.reconcile_once = _raise_reschedule

    real_try_load = main_mod._try_load_step

    def _patched_try_load(name):
        if name == "reconcile":
            return fake_reconcile
        if name == "applier":
            return applier
        return real_try_load(name)

    monkeypatch.setattr(main_mod, "_try_load_step", _patched_try_load)

    rc = main_mod.run_pass(repo_root=tmp_path, pass_id="2026-05-22-pass-reschedule-exit")

    assert rc == applier.EXIT_RESCHEDULE, (
        f"run_pass must return the reschedule exit code on RescheduleError, got {rc}"
    )
    # The reschedule code is observably distinct from the success/error codes
    # emitted by the very same run_pass return-code seam.
    assert rc != 0 and rc != 1


def test_reschedule_error_attributes(applier):
    """RescheduleError carries attempt_count and last_error attributes."""
    err = applier.RescheduleError(attempt_count=3, last_error="exhausted 3 attempts")
    assert err.attempt_count == 3, (
        f"RescheduleError.attempt_count must be 3, got {err.attempt_count}"
    )
    assert err.last_error == "exhausted 3 attempts", (
        f"RescheduleError.last_error mismatch: {err.last_error!r}"
    )
    # Must be a proper Exception subclass so callers can catch it
    assert isinstance(err, Exception), "RescheduleError must be an Exception subclass"
