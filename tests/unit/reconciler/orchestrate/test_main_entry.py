"""Check entrypoint pass delegation, repo-root threading, and success/reschedule exit
codes. Advisory locking is tested separately.
"""

from __future__ import annotations

import importlib
import subprocess
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Module loading
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[4]
MAIN_PATH = REPO_ROOT / "src" / "rebar" / "reconciler" / "__main__.py"
RECONCILER_PKG = REPO_ROOT / "src" / "rebar" / "reconciler"


def _load_main_module():
    """Load __main__.py as a named module for patching."""
    return importlib.import_module("rebar.reconciler.__main__")


@pytest.fixture(scope="module")
def main_mod():
    """Load the __main__ module, failing all tests if absent."""
    if not MAIN_PATH.exists():
        pytest.fail(
            f"__main__.py not found at {MAIN_PATH} — implement the module to make tests pass."
        )
    return _load_main_module()


@pytest.fixture(scope="module")
def guarded_mod():
    """The guarded-pass module that owns the pass body and its pipeline-load seams."""
    return importlib.import_module("rebar.reconciler.guarded_pass")


@pytest.fixture(autouse=True)
def _git_repo(tmp_path: Path) -> None:
    """Mutating main-path tests provide the git ref store finalization requires."""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    tracker = tmp_path / ".tickets-tracker"
    tracker.mkdir()
    (tracker / ".env-id").write_text("test-local\n", encoding="utf-8")


def _make_stub_reconcile(return_value=None, side_effect=None) -> types.ModuleType:
    """Return a stub reconcile module exposing reconcile_once as a MagicMock."""
    stub = types.ModuleType("stub_reconcile")
    if side_effect is not None:
        stub.reconcile_once = MagicMock(side_effect=side_effect)  # type: ignore[attr-defined]
    else:
        rv = (
            return_value
            if return_value is not None
            else {
                "pass_id": "test-pass",
                "mutation_count": 0,
                "manifest_path": "/tmp/manifest.json",
            }
        )
        stub.reconcile_once = MagicMock(return_value=rv)  # type: ignore[attr-defined]
    return stub


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_run_pass_returns_0_when_reconcile_succeeds(guarded_mod, tmp_path):
    """run_pass_result() is converged (exit 0) when reconcile_once returns a result dict."""
    stub = _make_stub_reconcile(
        return_value={
            "pass_id": "p-001",
            "mutation_count": 3,
            "manifest_path": str(tmp_path / "manifest.json"),
        }
    )

    with patch.object(guarded_mod, "_try_load_step", return_value=stub):
        result = guarded_mod.run_pass_result(repo_root=tmp_path)

    assert result.disposition is guarded_mod._Disposition.CONVERGED
    assert result.disposition.legacy_exit == 0
    stub.reconcile_once.assert_called_once()
    # reconcile_once must receive repo_root from run_pass_result
    call_kwargs = stub.reconcile_once.call_args.kwargs
    assert call_kwargs.get("repo_root") == tmp_path


def test_run_pass_returns_1_when_reconcile_raises(main_mod, guarded_mod, tmp_path, capsys):
    """The rendered pass returns 1 and does not re-raise when reconcile_once raises."""
    stub = _make_stub_reconcile(side_effect=RuntimeError("boom"))

    with patch.object(guarded_mod, "_try_load_step", return_value=stub):
        rc = main_mod._render_pass(guarded_mod.run_pass_result(repo_root=tmp_path), None)

    captured = capsys.readouterr()
    assert rc == 1
    assert "ERROR: reconcile_once raised: boom" in captured.err
    assert "OK:" not in captured.out
    assert "OK:" not in captured.err


def test_run_pass_returns_75_on_reschedule_error(guarded_mod, tmp_path):
    """Propagate RescheduleError as exit 75 so the scheduler retries instead of
    treating it as a generic failure.
    """
    # Use the real applier so we use the real RescheduleError and EXIT_RESCHEDULE
    applier_mod = importlib.import_module("rebar.reconciler.applier")

    stub_reconcile = _make_stub_reconcile(
        side_effect=applier_mod.RescheduleError(attempt_count=3, last_error="exhausted")
    )

    def _load_step(name):
        if name == "reconcile":
            return stub_reconcile
        if name == "applier":
            return applier_mod
        return None

    with patch.object(guarded_mod, "_try_load_step", side_effect=_load_step):
        result = guarded_mod.run_pass_result(repo_root=tmp_path)

    assert result.disposition is guarded_mod._Disposition.RESCHEDULE
    rc = result.disposition.legacy_exit
    assert rc == applier_mod.EXIT_RESCHEDULE, (
        f"the legacy route must exit EXIT_RESCHEDULE (75) when reconcile_once raises "
        f"RescheduleError; got {rc}"
    )
    assert rc == 75, f"EXIT_RESCHEDULE must equal 75; got {rc}"


def test_canonical_run_pass_maps_reschedule_to_benign_zero(main_mod, guarded_mod, tmp_path, capsys):
    """Canonical sync names a reschedule and exposes only the 0/1/2 contract."""
    applier_mod = importlib.import_module("rebar.reconciler.applier")
    stub_reconcile = _make_stub_reconcile(
        side_effect=applier_mod.RescheduleError(attempt_count=3, last_error="exhausted")
    )

    def _load_step(name):
        if name == "reconcile":
            return stub_reconcile
        if name == "applier":
            return applier_mod
        return None

    with (
        patch.object(guarded_mod, "_try_load_step", side_effect=_load_step),
        patch.object(guarded_mod, "_project_visibility_preflight", return_value=None),
    ):
        result = guarded_mod.run_pass_result(repo_root=tmp_path, route="sync")
        rc = main_mod._render_pass(result, "sync")

    assert rc == 0
    assert capsys.readouterr().err == "BRIDGE_STATE: reschedule\n"


@pytest.mark.parametrize("missing", ["reconcile_step", "advisory_lock"])
def test_missing_pipeline_module_raises_instead_of_converging(guarded_mod, tmp_path, missing):
    """A missing pipeline module is an import failure, never a converged pass.

    The by-path loaders once turned an absent ``reconcile.py`` into a graceful no-op and
    an ``_advisory_lock`` ImportError into ``None``. Both fallbacks were deleted with the
    move to ``rebar.reconciler``, so the ImportError must propagate out of
    ``run_pass_result`` before ``reconcile_once`` runs.
    """
    stub_reconcile = _make_stub_reconcile()
    real_try_load_step = guarded_mod._try_load_step
    real_load_sibling_keyed = guarded_mod._load_sibling_keyed

    def _load_step(name):
        if name == "reconcile":
            if missing == "reconcile_step":
                raise ModuleNotFoundError("No module named 'rebar.reconciler.reconcile'")
            return stub_reconcile
        return real_try_load_step(name)

    def _load_sibling(key, filename):
        if missing == "advisory_lock" and key == guarded_mod._ADVISORY_LOCK_KEY:
            raise ModuleNotFoundError(f"No module named {key!r}")
        return real_load_sibling_keyed(key, filename)

    with (
        patch.object(guarded_mod, "_try_load_step", side_effect=_load_step),
        patch.object(guarded_mod, "_load_sibling_keyed", side_effect=_load_sibling),
        patch.object(guarded_mod, "_project_visibility_preflight", return_value=None),
        pytest.raises(ImportError),
    ):
        guarded_mod.run_pass_result(repo_root=tmp_path, route="sync")

    stub_reconcile.reconcile_once.assert_not_called()


def test_main_returns_0_when_reconcile_succeeds(main_mod, guarded_mod, tmp_path):
    """Thread --repo-root through the guarded pass and return its successful exit code."""
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
    class _FakeMode:
        value = "live"

        @classmethod
        def from_str(cls, v):
            return cls()

        def __gt__(self, other):
            return False

        LIVE = None  # patched below
        RECONCILE_CHECK = "reconcile-check-sentinel"  # sentinel; never == _FakeMode()

    _FakeMode.LIVE = _FakeMode()
    mode_stub.Mode = _FakeMode

    request_stub = types.ModuleType("_request_stub")
    request_stub.RequestError = ValueError
    request_stub.normalize_request = MagicMock(
        return_value=types.SimpleNamespace(
            route=None,
            repo_root=tmp_path,
            target_mode=_FakeMode.LIVE,
            dry_run_enumerate=False,
            selection_tokens=(),
            filter_local_ids=None,
            selection_kind=None,
            max_changes=None,
        )
    )

    def _fake_load_sibling(key, filename):
        if "advisory" in filename:
            return advisory_stub
        if "mode" in filename:
            return mode_stub
        if "request" in filename:
            return request_stub
        raise ImportError(f"Unexpected _load_sibling_keyed call: {filename}")

    with (
        patch.object(guarded_mod, "_try_load_step", return_value=stub),
        patch.object(guarded_mod, "_project_visibility_preflight", return_value=None),
        patch.object(guarded_mod, "_load_sibling_keyed", side_effect=_fake_load_sibling),
    ):
        rc = main_mod.main(["--repo-root", str(tmp_path)])

    assert rc == 0
    stub.reconcile_once.assert_called_once()
    call_kwargs = stub.reconcile_once.call_args.kwargs
    assert call_kwargs.get("repo_root") == tmp_path
