"""Behavioral tests for rebar_reconciler.__main__ mode-validation + advisory-lock guards.

These tests verify the guard sequence in main():
  argparse → Mode.from_str → check_pass_lock → check_phase_gate →
  acquire_pass_lock → try/finally(release) → reconcile_once

Test-loading strategy:
  Modules are loaded via importlib.util.spec_from_file_location.
  The ``plugins`` directory is NOT a Python package on sys.path, so
  unittest.mock.patch() targets of the form
  ``"rebar_reconciler._advisory_lock.<fn>"``
  cannot resolve without pre-seeding sys.modules with namespace ModuleType
  entries.

  The module-scoped ``_seed_sys_modules`` fixture:
    1. Creates stub namespace entries for the intermediate package segments
       (the rebar_reconciler package).
    2. Loads _advisory_lock.py and mode.py under their fully-qualified dotted
       names via importlib so the real module objects live at the expected keys.
    3. Loads __main__.py under the same strategy, registering it at
       ``"rebar_reconciler.__main__"``.

  This guarantees that patch() targets resolve to the real module objects, and
  that production code (which also imports by dotted name) and test code see the
  same module object.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[4]
_PKG_DIR = REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler"
_MAIN_PATH = _PKG_DIR / "__main__.py"
_ADVISORY_LOCK_PATH = _PKG_DIR / "_advisory_lock.py"
_MODE_PATH = _PKG_DIR / "mode.py"

# Dotted module key that __main__.py will use when importing _advisory_lock
_ADVISORY_LOCK_KEY = "rebar_reconciler._advisory_lock"
_MODE_KEY = "rebar_reconciler.mode"
_MAIN_KEY = "rebar_reconciler.__main__"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _load_module(name: str, path: Path) -> types.ModuleType:
    """Load a file as a named module and register it in sys.modules."""
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None, f"Cannot load {path}"
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


# ---------------------------------------------------------------------------
# Module-scoped fixture: seed sys.modules and load modules under test
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def _seed_sys_modules(request):
    """Seed sys.modules so patch() targets resolve to real module objects.

    Returns a dict with keys 'main_mod', 'advisory_lock_mod', 'mode_mod'.

    Registers a finalizer to clean up the seeded namespace stub entries
    after this test module completes, so other test modules are not
    affected by the seeded stub entries.
    """
    # Track which keys we newly insert (vs those already present) so we only
    # clean up what WE added — leave pre-existing entries intact.
    newly_added: list[str] = []

    # Step 1: seed the rebar_reconciler namespace package
    for pkg in ("rebar_reconciler",):
        if pkg not in sys.modules:
            sys.modules[pkg] = types.ModuleType(pkg)
            newly_added.append(pkg)

    # Step 2: load real _advisory_lock and mode modules under the exact dotted keys
    advisory_lock_mod = _load_module(_ADVISORY_LOCK_KEY, _ADVISORY_LOCK_PATH)
    newly_added.append(_ADVISORY_LOCK_KEY)
    mode_mod = _load_module(_MODE_KEY, _MODE_PATH)
    newly_added.append(_MODE_KEY)

    # Step 3: load __main__ under its dotted key (also under the standard key)
    main_mod = _load_module(_MAIN_KEY, _MAIN_PATH)
    newly_added.append(_MAIN_KEY)
    sys.modules["rebar_reconciler.__main__"] = main_mod  # keep existing consumers happy
    newly_added.append("rebar_reconciler.__main__")

    def _cleanup():
        for key in newly_added:
            sys.modules.pop(key, None)

    request.addfinalizer(_cleanup)

    return {
        "main_mod": main_mod,
        "advisory_lock_mod": advisory_lock_mod,
        "mode_mod": mode_mod,
    }


@pytest.fixture
def main_mod(_seed_sys_modules):
    """Return the loaded __main__ module."""
    return _seed_sys_modules["main_mod"]


# ---------------------------------------------------------------------------
# Fetcher sentinel: a module-level MagicMock placed in sys.modules so we can
# detect whether reconcile_once (and therefore the fetcher) was ever invoked.
# ---------------------------------------------------------------------------


@pytest.fixture
def fetcher_sentinel():
    """Install a MagicMock for the fetcher module in sys.modules.

    Returns the mock so tests can assert call_count == 0.
    """
    sentinel = MagicMock()
    sentinel.fetch_snapshot = MagicMock(return_value=None)
    original = sys.modules.get("reconcile_fetcher")
    sys.modules["reconcile_fetcher"] = sentinel
    yield sentinel
    # Restore
    if original is None:
        sys.modules.pop("reconcile_fetcher", None)
    else:
        sys.modules["reconcile_fetcher"] = original


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_unknown_mode_exits_before_fetcher(main_mod, fetcher_sentinel, tmp_path):
    """main(['--mode=not-a-mode']) exits non-zero; stderr names all 4 allowed modes;
    fetcher sentinel call_count == 0 (no fetcher call made before mode-validation).
    """
    import io

    stderr_buf = io.StringIO()
    with patch("sys.stderr", stderr_buf):
        rc = main_mod.main(["--mode=not-a-mode", "--repo-root", str(tmp_path)])

    assert rc != 0, f"Expected non-zero rc for unknown mode, got {rc}"

    stderr_output = stderr_buf.getvalue()
    for allowed in ("dry-run", "bootstrap-strict", "bootstrap-throttle", "live"):
        assert allowed in stderr_output, (
            f"Expected allowed mode {allowed!r} in stderr; got: {stderr_output!r}"
        )

    assert fetcher_sentinel.fetch_snapshot.call_count == 0, (
        "fetcher.fetch_snapshot must NOT be called before mode-validation"
    )


def test_pass_lock_blocks_before_fetcher(main_mod, fetcher_sentinel, tmp_path):
    """main(['--mode=dry-run']) with check_pass_lock returning True exits non-zero;
    fetcher sentinel call_count == 0.
    """
    with patch(
        f"{_ADVISORY_LOCK_KEY}.check_pass_lock",
        return_value=True,
    ):
        rc = main_mod.main(["--mode=dry-run", "--repo-root", str(tmp_path)])

    assert rc != 0, f"Expected non-zero rc when pass-lock is held, got {rc}"
    assert fetcher_sentinel.fetch_snapshot.call_count == 0, (
        "fetcher.fetch_snapshot must NOT be called when pass-lock is held"
    )


def test_phase_gate_requires_removal_to_advance(main_mod, fetcher_sentinel, tmp_path):
    """main(['--mode=bootstrap-throttle']) exits non-zero when check_phase_gate
    returns True; then proceeds (calls reconcile_once) when check_phase_gate
    returns False.
    """
    # First: phase gate present → blocked
    with (
        patch(
            f"{_ADVISORY_LOCK_KEY}.check_pass_lock",
            return_value=False,
        ),
        patch(
            f"{_ADVISORY_LOCK_KEY}.check_phase_gate",
            return_value=True,
        ),
    ):
        rc_blocked = main_mod.main(["--mode=bootstrap-throttle", "--repo-root", str(tmp_path)])

    assert rc_blocked != 0, f"Expected non-zero rc when phase gate blocks, got {rc_blocked}"

    # Second: phase gate absent + full pass mock → proceeds
    stub_reconcile = types.ModuleType("stub_reconcile_phase_gate")
    stub_reconcile.reconcile_once = MagicMock(
        return_value={
            "pass_id": "p1",
            "mutation_count": 0,
            "manifest_path": "/tmp/m.json",
        }
    )

    with (
        patch(
            f"{_ADVISORY_LOCK_KEY}.check_pass_lock",
            return_value=False,
        ),
        patch(
            f"{_ADVISORY_LOCK_KEY}.check_phase_gate",
            return_value=False,
        ),
        patch(
            f"{_ADVISORY_LOCK_KEY}.acquire_pass_lock",
            return_value=None,
        ),
        patch(
            f"{_ADVISORY_LOCK_KEY}.release_pass_lock",
            return_value=None,
        ),
        patch.object(main_mod, "_try_load_step", return_value=stub_reconcile),
    ):
        rc_open = main_mod.main(["--mode=bootstrap-throttle", "--repo-root", str(tmp_path)])

    assert rc_open == 0, (
        f"Expected 0 rc when phase gate is open and reconcile_once succeeds, got {rc_open}"
    )
    stub_reconcile.reconcile_once.assert_called_once()


def test_lock_released_on_exception(main_mod, tmp_path):
    """When reconcile_once raises, release_pass_lock is still called (finally block)."""
    release_mock = MagicMock()
    stub_reconcile = types.ModuleType("stub_reconcile_exc")
    stub_reconcile.reconcile_once = MagicMock(side_effect=RuntimeError("boom"))

    with (
        patch(
            f"{_ADVISORY_LOCK_KEY}.check_pass_lock",
            return_value=False,
        ),
        patch(
            f"{_ADVISORY_LOCK_KEY}.check_phase_gate",
            return_value=False,
        ),
        patch(
            f"{_ADVISORY_LOCK_KEY}.acquire_pass_lock",
            return_value=None,
        ),
        patch(
            f"{_ADVISORY_LOCK_KEY}.release_pass_lock",
            release_mock,
        ),
        patch.object(main_mod, "_try_load_step", return_value=stub_reconcile),
    ):
        rc = main_mod.main(["--mode=dry-run", "--repo-root", str(tmp_path)])

    assert rc != 0, f"Expected non-zero rc when reconcile_once raises, got {rc}"
    assert release_mock.call_count >= 1, (
        "release_pass_lock must be called in the finally block even on exception"
    )


@pytest.mark.parametrize(
    "exc_factory,exc_id",
    [
        (lambda: RuntimeError("boom"), "RuntimeError"),
        # SystemExit bypasses bare `except Exception:` blocks because it
        # inherits from BaseException, not Exception. Only a try/finally
        # (not try/except) will release the lock on this path.
        (lambda: SystemExit(2), "SystemExit"),
    ],
)
def test_lock_released_on_exception_variants(main_mod, tmp_path, exc_factory, exc_id):
    """release_pass_lock is called in finally on RuntimeError AND on SystemExit.

    SystemExit is the critical edge case: it inherits from BaseException, so
    any `except Exception:` block would silently let it propagate WITHOUT
    releasing the advisory lock. The finally block in main() is the only
    safety net.
    """
    release_mock = MagicMock()
    stub_reconcile = types.ModuleType(f"stub_reconcile_exc_{exc_id}")
    stub_reconcile.reconcile_once = MagicMock(side_effect=exc_factory())

    raised: BaseException | None = None
    with (
        patch(
            f"{_ADVISORY_LOCK_KEY}.check_pass_lock",
            return_value=False,
        ),
        patch(
            f"{_ADVISORY_LOCK_KEY}.check_phase_gate",
            return_value=False,
        ),
        patch(
            f"{_ADVISORY_LOCK_KEY}.acquire_pass_lock",
            return_value=None,
        ),
        patch(
            f"{_ADVISORY_LOCK_KEY}.release_pass_lock",
            release_mock,
        ),
        patch.object(main_mod, "_try_load_step", return_value=stub_reconcile),
    ):
        try:
            main_mod.main(["--mode=dry-run", "--repo-root", str(tmp_path)])
        except BaseException as e:
            # SystemExit may propagate out of main() — that's an acceptable
            # outcome; we only require that release_pass_lock ran.
            raised = e

    # The lock must be released regardless of exception class.
    assert release_mock.call_count >= 1, (
        f"release_pass_lock must be called in the finally block on {exc_id}; "
        f"call_count={release_mock.call_count}, raised={raised!r}"
    )


def test_import_does_not_load_fetcher(_seed_sys_modules):
    """Importing the reconcile module does NOT pull fetcher into sys.modules.

    Verifies that reconcile.py uses lazy _load() calls for fetcher (deferred
    until reconcile_once is called), not top-level imports.
    """
    # Remove any pre-existing fetcher entry from a prior test run
    for key in list(sys.modules.keys()):
        if "fetcher" in key.lower() and "reconcile" in key.lower():
            del sys.modules[key]

    # Load reconcile.py fresh (as a new key to avoid collision with existing)
    reconcile_path = _PKG_DIR / "reconcile.py"
    _load_module("_test_import_reconcile_fresh", reconcile_path)

    # Fetcher should NOT be in sys.modules after a bare import
    fetcher_loaded = any(
        "fetcher" in k.lower() and "reconcile" in k.lower()
        for k in sys.modules
        if k != "_test_import_reconcile_fresh"
    )
    assert not fetcher_loaded, (
        "reconcile.py must not load fetcher at module-import time (lazy import topology)"
    )


def test_no_mode_flag_defaults_to_live(main_mod, tmp_path):
    """main([]) with no --mode flag defaults to Mode.LIVE and proceeds normally."""
    stub_reconcile = types.ModuleType("stub_reconcile_live")
    stub_reconcile.reconcile_once = MagicMock(
        return_value={
            "pass_id": "p-live",
            "mutation_count": 0,
            "manifest_path": "/tmp/m.json",
        }
    )

    with (
        patch(
            f"{_ADVISORY_LOCK_KEY}.check_pass_lock",
            return_value=False,
        ),
        patch(
            f"{_ADVISORY_LOCK_KEY}.check_phase_gate",
            return_value=False,
        ),
        patch(
            f"{_ADVISORY_LOCK_KEY}.acquire_pass_lock",
            return_value=None,
        ),
        patch(
            f"{_ADVISORY_LOCK_KEY}.release_pass_lock",
            return_value=None,
        ),
        patch.object(main_mod, "_try_load_step", return_value=stub_reconcile),
    ):
        rc = main_mod.main(["--repo-root", str(tmp_path)])

    assert rc == 0, f"Expected 0 rc when no --mode flag given (defaults to live), got {rc}"
    stub_reconcile.reconcile_once.assert_called_once()


def test_main_without_repo_root_does_not_pass_none_to_advisory(main_mod):
    """main(['--mode=dry-run']) without --repo-root must NOT pass None to check_pass_lock.

    Bug 5be7: __main__.py:151 left repo_root=None when --repo-root was omitted.
    That None propagated into advisory.check_pass_lock(None) → _git_show_tickets_file
    → subprocess.run(['git', '-C', 'None', 'show', ...]) → exit 128 → ReconcileLockError.

    This test exercises the call-site contract: the first positional argument
    received by check_pass_lock must be a Path instance, not None, and its
    string representation must not be the literal 'None'.
    """
    check_pass_lock_mock = MagicMock(return_value=False)

    with (
        patch(
            f"{_ADVISORY_LOCK_KEY}.check_pass_lock",
            check_pass_lock_mock,
        ),
        patch(
            f"{_ADVISORY_LOCK_KEY}.check_phase_gate",
            return_value=False,
        ),
        patch(
            f"{_ADVISORY_LOCK_KEY}.acquire_pass_lock",
            return_value=None,
        ),
        patch(
            f"{_ADVISORY_LOCK_KEY}.release_pass_lock",
            return_value=None,
        ),
        patch.object(
            main_mod,
            "_try_load_step",
            return_value=types.SimpleNamespace(
                reconcile_once=MagicMock(
                    return_value={
                        "pass_id": "p-test",
                        "mutation_count": 0,
                        "manifest_path": "/tmp/m.json",
                    }
                )
            ),
        ),
    ):
        main_mod.main(["--mode=dry-run"])

    assert check_pass_lock_mock.call_count >= 1, "check_pass_lock must be called when main() runs"
    actual_repo_root_arg = check_pass_lock_mock.call_args[0][0]
    assert isinstance(actual_repo_root_arg, Path), (
        f"check_pass_lock must receive a Path, not {type(actual_repo_root_arg).__name__!r} "
        f"(value: {actual_repo_root_arg!r}); bug 5be7 left repo_root=None when --repo-root omitted"
    )
    assert str(actual_repo_root_arg) != "None", (
        "check_pass_lock received the literal string 'None' — repo_root was not resolved; "
        "bug 5be7: Path(None) produces Path('None'), not a real directory path"
    )


def test_main_without_repo_root_passes_resolved_repo_root(main_mod, monkeypatch):
    """The Path passed to check_pass_lock when --repo-root is omitted must contain
    the rebar_reconciler package, confirming it resolves to the actual project root.

    The conftest sandbox sets REBAR_ROOT for isolation; clear it
    here so the genuine depth-fallback runs. reconcile_once is mocked, so nothing
    is written to the resolved root.

    This pins the default-resolution path: Path(__file__).resolve().parents[4]
    from __main__.py should reach the repo root, which contains
    src/rebar/_engine/rebar_reconciler/__main__.py.
    """
    monkeypatch.delenv("REBAR_ROOT", raising=False)
    check_pass_lock_mock = MagicMock(return_value=False)

    with (
        patch(
            f"{_ADVISORY_LOCK_KEY}.check_pass_lock",
            check_pass_lock_mock,
        ),
        patch(
            f"{_ADVISORY_LOCK_KEY}.check_phase_gate",
            return_value=False,
        ),
        patch(
            f"{_ADVISORY_LOCK_KEY}.acquire_pass_lock",
            return_value=None,
        ),
        patch(
            f"{_ADVISORY_LOCK_KEY}.release_pass_lock",
            return_value=None,
        ),
        patch.object(
            main_mod,
            "_try_load_step",
            return_value=types.SimpleNamespace(
                reconcile_once=MagicMock(
                    return_value={
                        "pass_id": "p-test2",
                        "mutation_count": 0,
                        "manifest_path": "/tmp/m.json",
                    }
                )
            ),
        ),
    ):
        main_mod.main(["--mode=dry-run"])

    assert check_pass_lock_mock.call_count >= 1, "check_pass_lock must be called when main() runs"
    actual_repo_root_arg = check_pass_lock_mock.call_args[0][0]
    assert isinstance(actual_repo_root_arg, Path), (
        f"Expected a Path, got {type(actual_repo_root_arg).__name__!r}: {actual_repo_root_arg!r}"
    )
    # The resolved default must point at a directory containing the rebar_reconciler package,
    # confirming it is the actual project repo root (not an arbitrary or null path).
    expected_marker = (
        actual_repo_root_arg / "src" / "rebar" / "_engine" / "rebar_reconciler" / "__main__.py"
    )
    assert expected_marker.exists(), (
        f"Resolved repo_root {actual_repo_root_arg!r} does not contain "
        f"src/rebar/_engine/rebar_reconciler/__main__.py — default root resolution is wrong; "
        f"expected Path(__file__).resolve().parents[4] from __main__.py"
    )
