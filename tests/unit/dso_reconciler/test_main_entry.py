"""Behavioral tests for dso_reconciler.__main__ entry module.

Tests cover the canonical __main__ API which delegates a single steady-state
pass to reconcile.reconcile_once(pass_id, repo_root=...):

  - test_run_pass_returns_0_when_all_steps_absent: run_pass() returns 0 directly
    when reconcile.py is not loadable (walking-skeleton no-op path).
  - test_run_pass_returns_0_when_reconcile_succeeds: run_pass() returns 0 when a
    stubbed reconcile.reconcile_once returns a normal result dict.
  - test_run_pass_returns_1_when_reconcile_raises: run_pass() returns 1 and does
    not propagate the exception when reconcile.reconcile_once raises.
  - test_main_returns_0_when_reconcile_succeeds: main() with --repo-root threads
    through to run_pass() and returns 0.
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
MAIN_PATH = (
    REPO_ROOT / "src" / "rebar" / "_engine" / "dso_reconciler" / "__main__.py"
)
RECONCILER_PKG = REPO_ROOT / "src" / "rebar" / "_engine" / "dso_reconciler"


def _load_main_module():
    """Load __main__.py as a named module for patching."""
    spec = importlib.util.spec_from_file_location("dso_reconciler.__main__", MAIN_PATH)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules["dso_reconciler.__main__"] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def main_mod():
    """Load the __main__ module, failing all tests if absent."""
    if not MAIN_PATH.exists():
        pytest.fail(
            f"__main__.py not found at {MAIN_PATH} — "
            "implement the module to make tests pass."
        )
    return _load_main_module()


def _make_stub_reconcile(return_value=None, side_effect=None) -> types.ModuleType:
    """Return a stub reconcile module exposing reconcile_once as a MagicMock."""
    stub = types.ModuleType("stub_reconcile")
    if side_effect is not None:
        stub.reconcile_once = MagicMock(side_effect=side_effect)  # type: ignore[attr-defined]
    else:
        rv = return_value if return_value is not None else {
            "pass_id": "test-pass",
            "mutation_count": 0,
            "manifest_path": "/tmp/manifest.json",
        }
        stub.reconcile_once = MagicMock(return_value=rv)  # type: ignore[attr-defined]
    return stub


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_run_pass_returns_0_when_all_steps_absent(main_mod, tmp_path):
    """run_pass() returns 0 when _try_load_step returns None for reconcile.

    Simulates the walking-skeleton path: reconcile.py is not present.  Per
    __main__.py's module docstring, modules that do not exist yet are skipped
    gracefully and the pass converges to a clean 0 exit.
    """
    with patch.object(main_mod, "_try_load_step", return_value=None):
        rc = main_mod.run_pass(repo_root=tmp_path)
    assert rc == 0


def test_run_pass_returns_0_when_reconcile_succeeds(main_mod, tmp_path):
    """run_pass() returns 0 when reconcile.reconcile_once returns a result dict."""
    stub = _make_stub_reconcile(return_value={
        "pass_id": "p-001",
        "mutation_count": 3,
        "manifest_path": str(tmp_path / "manifest.json"),
    })

    with patch.object(main_mod, "_try_load_step", return_value=stub):
        rc = main_mod.run_pass(repo_root=tmp_path)

    assert rc == 0
    stub.reconcile_once.assert_called_once()
    # reconcile_once must receive repo_root from run_pass
    call_kwargs = stub.reconcile_once.call_args.kwargs
    assert call_kwargs.get("repo_root") == tmp_path


def test_run_pass_returns_1_when_reconcile_raises(main_mod, tmp_path):
    """run_pass() returns 1 and does not re-raise when reconcile_once raises."""
    stub = _make_stub_reconcile(side_effect=RuntimeError("boom"))

    with patch.object(main_mod, "_try_load_step", return_value=stub):
        rc = main_mod.run_pass(repo_root=tmp_path)

    assert rc == 1


def test_run_pass_returns_75_on_reschedule_error(main_mod, tmp_path):
    """F6 regression: RescheduleError must surface as EXIT_RESCHEDULE (75), not 1.

    Before F6, run_pass swallowed RescheduleError under the broad
    ``except Exception`` and returned 1, hiding the reschedule signal from
    any scheduler that distinguishes 75 from 1. The fix loads the applier
    module, special-cases its RescheduleError, and returns
    applier.EXIT_RESCHEDULE (75).
    """
    # Load the real applier so we use the real RescheduleError and EXIT_RESCHEDULE
    import importlib.util

    applier_path = (
        REPO_ROOT
        / "src"
        / "rebar"
        / "_engine"
        / "dso_reconciler"
        / "applier.py"
    )
    spec = importlib.util.spec_from_file_location("applier_for_reschedule_test", applier_path)
    applier_mod = importlib.util.module_from_spec(spec)
    # Python 3.14: dataclass introspection reads sys.modules.get(cls.__module__).__dict__
    # during class construction; the module must be registered before exec.
    import sys as _sys
    _sys.modules["applier_for_reschedule_test"] = applier_mod
    spec.loader.exec_module(applier_mod)

    stub_reconcile = _make_stub_reconcile(
        side_effect=applier_mod.RescheduleError(attempt_count=3, last_error="exhausted")
    )

    def _load_step(name):
        if name == "reconcile":
            return stub_reconcile
        if name == "applier":
            return applier_mod
        return None

    with patch.object(main_mod, "_try_load_step", side_effect=_load_step):
        rc = main_mod.run_pass(repo_root=tmp_path)

    assert rc == applier_mod.EXIT_RESCHEDULE, (
        f"run_pass must return EXIT_RESCHEDULE (75) when reconcile_once raises "
        f"RescheduleError; got {rc}"
    )
    assert rc == 75, f"EXIT_RESCHEDULE must equal 75; got {rc}"


def test_main_returns_0_when_reconcile_succeeds(main_mod, tmp_path):
    """main() threads --repo-root through to run_pass and returns its exit code.

    Advisory-lock guard calls are mocked out so this test remains a pure
    unit test of the run_pass delegation path; lock integration is covered
    by test_reconcile_main.py.
    """
    stub = _make_stub_reconcile()

    # Mock _load_sibling_keyed so main() gets a stub advisory module that
    # reports no lock and no phase gate, preventing real git calls on tmp_path.
    advisory_stub = types.ModuleType("_advisory_lock_stub")
    advisory_stub.check_pass_lock = MagicMock(return_value=False)
    advisory_stub.check_phase_gate = MagicMock(return_value=False)
    advisory_stub.acquire_pass_lock = MagicMock(return_value=None)
    advisory_stub.release_pass_lock = MagicMock(return_value=None)

    mode_stub = types.ModuleType("_mode_stub")
    # Provide a real Mode-like object for LIVE
    class _FakeMode:  # noqa: N801
        value = "live"
        @classmethod
        def from_str(cls, v):
            return cls()
        def rank(self):
            return 3
        LIVE = None  # patched below
        RECONCILE_CHECK = "reconcile-check-sentinel"  # sentinel; never == _FakeMode()
    _FakeMode.LIVE = _FakeMode()
    mode_stub.Mode = _FakeMode

    def _fake_load_sibling(key, filename):
        if "advisory" in filename:
            return advisory_stub
        if "mode" in filename:
            return mode_stub
        raise ImportError(f"Unexpected _load_sibling_keyed call: {filename}")

    with patch.object(main_mod, "_try_load_step", return_value=stub), \
         patch.object(main_mod, "_load_sibling_keyed", side_effect=_fake_load_sibling):
        rc = main_mod.main(["--repo-root", str(tmp_path)])

    assert rc == 0
    stub.reconcile_once.assert_called_once()
    call_kwargs = stub.reconcile_once.call_args.kwargs
    assert call_kwargs.get("repo_root") == tmp_path
