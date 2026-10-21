"""Behavioral tests for rebar.reconciler.__main__ mode-validation + advisory-lock guards.

These tests verify the guard sequence main() runs through guarded_pass.run_guarded_pass:
  argparse → Mode.from_str → check_pass_lock → check_phase_gate →
  acquire_pass_lock → try/finally(release) → reconcile_once

Test-loading strategy:
  The modules under test are the shared ``rebar.reconciler`` submodules, so
  ``unittest.mock.patch()`` targets of the form
  ``"rebar.reconciler._advisory_lock.<fn>"`` resolve to the same objects that
  production code imports.
"""

from __future__ import annotations

import importlib
import importlib.util
import subprocess
import sys
import types
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[4]
_PKG_DIR = REPO_ROOT / "src" / "rebar" / "reconciler"
_MAIN_PATH = _PKG_DIR / "__main__.py"
_ADVISORY_LOCK_PATH = _PKG_DIR / "_advisory_lock.py"
_MODE_PATH = _PKG_DIR / "mode.py"

# Dotted module key that __main__.py will use when importing _advisory_lock
_ADVISORY_LOCK_KEY = "rebar.reconciler._advisory_lock"
_MODE_KEY = "rebar.reconciler.mode"
_MAIN_KEY = "rebar.reconciler.__main__"
_GUARDED_KEY = "rebar.reconciler.guarded_pass"


@pytest.fixture(autouse=True)
def _git_repo(tmp_path: Path) -> None:
    """Mutating main-path tests provide the git ref store finalization requires."""
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    tracker = tmp_path / ".tickets-tracker"
    tracker.mkdir()
    (tracker / ".env-id").write_text("test-local\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Module-scoped fixture: seed sys.modules and load modules under test
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def _seed_sys_modules():
    """The shared ``__main__``, ``guarded_pass``, ``_advisory_lock`` and ``mode`` modules.

    Returns a dict with keys 'main_mod', 'guarded_mod', 'advisory_lock_mod', 'mode_mod'.
    """
    return {
        "main_mod": importlib.import_module(_MAIN_KEY),
        "guarded_mod": importlib.import_module(_GUARDED_KEY),
        "advisory_lock_mod": importlib.import_module(_ADVISORY_LOCK_KEY),
        "mode_mod": importlib.import_module(_MODE_KEY),
    }


@pytest.fixture
def main_mod(_seed_sys_modules):
    """Return the loaded __main__ module."""
    return _seed_sys_modules["main_mod"]


@pytest.fixture
def guarded_mod(_seed_sys_modules):
    """Return the guarded-pass module that owns the pipeline-load seams."""
    return _seed_sys_modules["guarded_mod"]


# ---------------------------------------------------------------------------
# Fetcher sentinel: the shared fetcher's ``fetch_snapshot`` patched with a
# MagicMock so we can detect whether reconcile_once (and therefore the fetcher)
# was ever invoked.
# ---------------------------------------------------------------------------


@pytest.fixture
def fetcher_sentinel():
    """Patch the shared fetcher's ``fetch_snapshot``.

    Returns the mock so tests can assert call_count == 0.
    """
    fetcher = importlib.import_module("rebar.reconciler.fetcher")
    with patch.object(fetcher, "fetch_snapshot", return_value=None) as sentinel:
        yield type("_FetcherSentinel", (), {"fetch_snapshot": sentinel})


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


def test_phase_gate_requires_removal_to_advance(main_mod, guarded_mod, fetcher_sentinel, tmp_path):
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
        patch.object(guarded_mod, "_try_load_step", return_value=stub_reconcile),
    ):
        rc_open = main_mod.main(["--mode=bootstrap-throttle", "--repo-root", str(tmp_path)])

    assert rc_open == 0, (
        f"Expected 0 rc when phase gate is open and reconcile_once succeeds, got {rc_open}"
    )
    stub_reconcile.reconcile_once.assert_called_once()


@pytest.mark.parametrize(
    ("lock_held", "phase_gated", "expected_state"),
    [
        (True, False, "in-flight"),
        (False, True, "legacy-gated"),
    ],
)
def test_canonical_sync_maps_benign_preflight_to_zero(
    main_mod,
    guarded_mod,
    tmp_path,
    monkeypatch,
    capsys,
    lock_held: bool,
    phase_gated: bool,
    expected_state: str,
) -> None:
    """Canonical sync translates old benign guards without running a pass."""
    monkeypatch.setenv("REBAR_RECONCILER_LOCK_STEAL", "0")
    with (
        patch(f"{_ADVISORY_LOCK_KEY}.check_pass_lock", return_value=lock_held),
        patch(f"{_ADVISORY_LOCK_KEY}.check_phase_gate", return_value=phase_gated),
        patch.object(guarded_mod, "run_pass_result") as run_pass,
    ):
        rc = main_mod.main(["sync", "--repo-root", str(tmp_path)])

    assert rc == 0
    assert capsys.readouterr().err == f"BRIDGE_STATE: {expected_state}\n"
    run_pass.assert_not_called()


def test_removed_reconcile_check_mode_rejects_before_operational_work(
    main_mod, guarded_mod, tmp_path
):
    """The removed direct-engine diagnostic mode never reaches locks or a pass."""
    run_pass = MagicMock(side_effect=AssertionError("the pass must not run"))
    pass_lock = MagicMock(side_effect=AssertionError("pass lock must not be read"))
    pause_read = MagicMock(side_effect=AssertionError("pause gate must not be read"))

    with (
        patch.object(guarded_mod, "run_pass_result", run_pass),
        patch(f"{_ADVISORY_LOCK_KEY}.check_pass_lock", pass_lock),
        patch(f"{_ADVISORY_LOCK_KEY}.read_pause", pause_read),
    ):
        rc = main_mod.main(["--mode=reconcile-check", "--repo-root", str(tmp_path)])

    assert rc == 2
    run_pass.assert_not_called()
    pass_lock.assert_not_called()
    pause_read.assert_not_called()


def test_operation_snapshot_binding_does_not_leak_past_main_return(main_mod, guarded_mod, tmp_path):
    """Regression for the ticket-ec44 LLM-Review finding: an early ``compose_and_bind_
    operation_snapshot()`` CM whose ``__enter__`` return value nobody references is
    collectible immediately, which silently unwinds the binding right away — but the
    naive fix (keep the CM alive forever at module scope) trades that bug for a worse
    one, since this module is loaded and ``main()`` is invoked IN-PROCESS by tests
    sharing a single interpreter/pytest-xdist worker: a binding left active past
    ``main()``'s return would leak the bound snapshot's repo root into every later
    test in that process, exactly the cascading "ticket system not initialized"
    failures a leaked binding produces. Asserts BOTH halves of the contract: the
    snapshot is actively bound to THIS call's repo root while ``main()`` is still
    running, and it is fully unbound again once ``main()`` returns."""
    from rebar._operation_config import active_snapshot

    assert active_snapshot() is None, "no snapshot should be bound before this test runs"

    observed: dict[str, object] = {}

    def _run_preview(**kwargs):
        snapshot = active_snapshot()
        observed["snapshot"] = snapshot
        observed["repo_root"] = str(snapshot.repo_root) if snapshot is not None else None
        return guarded_mod.PassResult(guarded_mod._Disposition.CONVERGED, {})

    with patch.object(guarded_mod, "run_pass_result", _run_preview):
        rc = main_mod.main(["preview", "--repo-root", str(tmp_path)])

    assert rc == 0
    assert observed["snapshot"] is not None, "snapshot must be bound while main() runs"
    assert observed["repo_root"] == str(tmp_path)
    assert active_snapshot() is None, (
        "the binding must not survive main()'s return — a lingering binding leaks "
        "this call's repo root into every later test sharing this process"
    )


def test_lock_released_on_exception(main_mod, guarded_mod, tmp_path):
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
        patch.object(guarded_mod, "_try_load_step", return_value=stub_reconcile),
    ):
        rc = main_mod.main(["--mode=dry-run", "--repo-root", str(tmp_path)])

    assert rc != 0, f"Expected non-zero rc when reconcile_once raises, got {rc}"
    assert release_mock.call_count >= 1, (
        "release_pass_lock must be called in the finally block even on exception"
    )


@pytest.mark.parametrize(
    ("exc_factory", "exc_id"),
    [
        (lambda: RuntimeError("boom"), "RuntimeError"),
        # SystemExit bypasses bare `except Exception:` blocks because it
        # inherits from BaseException, not Exception. Only a try/finally
        # (not try/except) will release the lock on this path.
        (lambda: SystemExit(2), "SystemExit"),
    ],
)
def test_lock_released_on_exception_variants(main_mod, guarded_mod, tmp_path, exc_factory, exc_id):
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
        patch.object(guarded_mod, "_try_load_step", return_value=stub_reconcile),
    ):
        try:
            main_mod.main(["--mode=dry-run", "--repo-root", str(tmp_path)])
        except BaseException as e:  # noqa: BLE001 — deliberately catches SystemExit (a BaseException) from main(); asserts the finally-block lock release ran regardless of exit class
            # SystemExit may propagate out of main() — that's an acceptable
            # outcome; we only require that release_pass_lock ran.
            raised = e

    # The lock must be released regardless of exception class.
    assert release_mock.call_count >= 1, (
        f"release_pass_lock must be called in the finally block on {exc_id}; "
        f"call_count={release_mock.call_count}, raised={raised!r}"
    )


def test_import_does_not_load_fetcher(tmp_path):
    """Importing the reconcile module does NOT pull fetcher into sys.modules.

    Verifies that reconcile.py uses lazy _load() calls for fetcher (deferred
    until reconcile_once is called), not top-level imports. Checked in a fresh
    interpreter: in-process, other tests have already imported the shared fetcher.
    """
    probe = (
        "import sys\n"
        "import rebar.reconciler.reconcile\n"
        "loaded = sorted(k for k in sys.modules if 'fetcher' in k.lower())\n"
        "assert not loaded, loaded\n"
    )
    cp = subprocess.run(
        [sys.executable, "-c", probe], capture_output=True, text=True, check=False, cwd=tmp_path
    )
    assert cp.returncode == 0, (
        "reconcile.py must not load fetcher at module-import time (lazy import topology):\n"
        f"{cp.stdout}{cp.stderr}"
    )


def test_no_mode_flag_defaults_to_live(main_mod, guarded_mod, tmp_path):
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
        patch.object(guarded_mod, "_try_load_step", return_value=stub_reconcile),
    ):
        rc = main_mod.main(["--repo-root", str(tmp_path)])

    assert rc == 0, f"Expected 0 rc when no --mode flag given (defaults to live), got {rc}"
    stub_reconcile.reconcile_once.assert_called_once()


def test_main_without_repo_root_does_not_pass_none_to_advisory(main_mod, guarded_mod):
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
            guarded_mod,
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


def _stub_main_advisory(guarded_mod, extra_patches=()):
    """Stub EVERY advisory entry point ``main()`` reaches, plus the reconcile step.

    Returns ``(stack, check_pass_lock_mock, read_pause_mock)``; the caller enters *stack*.

    ``read_pause`` is stubbed for the same reason the others are, and the reason is specific
    to this pair of tests: they clear ``REBAR_ROOT`` deliberately, so ``main()`` resolves and
    runs against the REAL checkout rather than the suite's sandbox (``tests/conftest.py``
    defaults ``REBAR_ROOT`` for every other test — bug dd62 — and notes that a test which
    ``delenv``s it escapes that default). An advisory call left unstubbed therefore reads
    that checkout's live ``refs/reconciler/gate``. ``_pause_result`` runs BEFORE
    ``_post_pause_preflight``, so a gate ref that is corrupt (``ReconcileGateError``) or
    merely carries a pause marker makes ``main()`` return before ``check_pass_lock`` is ever
    called — failing the assertions below through no fault of the resolution under test.
    That is the CI flake in bug b82c-7461-0d95-44b8.
    """
    check_pass_lock_mock = MagicMock(return_value=False)
    read_pause_mock = MagicMock(return_value=None)
    stack = ExitStack()
    stack.enter_context(patch(f"{_ADVISORY_LOCK_KEY}.read_pause", read_pause_mock))
    stack.enter_context(patch(f"{_ADVISORY_LOCK_KEY}.check_pass_lock", check_pass_lock_mock))
    stack.enter_context(patch(f"{_ADVISORY_LOCK_KEY}.check_phase_gate", return_value=False))
    stack.enter_context(patch(f"{_ADVISORY_LOCK_KEY}.acquire_pass_lock", return_value=None))
    stack.enter_context(patch(f"{_ADVISORY_LOCK_KEY}.release_pass_lock", return_value=None))
    stack.enter_context(
        patch.object(
            guarded_mod,
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
        )
    )
    for extra in extra_patches:
        stack.enter_context(extra)
    return stack, check_pass_lock_mock, read_pause_mock


def _assert_resolved_repo_root(call_args) -> Path:
    """Assert *call_args* carries the genuine depth-fallback repo root, and return it."""
    resolved = call_args[0][0]
    assert isinstance(resolved, Path), (
        f"Expected a Path, got {type(resolved).__name__!r}: {resolved!r}"
    )
    # The resolved default must point at a directory containing the rebar.reconciler package,
    # confirming it is the actual project repo root (not an arbitrary or null path).
    marker = resolved / "src" / "rebar" / "reconciler" / "__main__.py"
    assert marker.exists(), (
        f"Resolved repo_root {resolved!r} does not contain "
        f"src/rebar/reconciler/__main__.py — default root resolution is wrong; "
        f"expected Path(__file__).resolve().parents[4] from __main__.py"
    )
    return resolved


def test_main_resolves_repo_root_even_when_the_ambient_gate_ref_is_corrupt(
    main_mod, guarded_mod, monkeypatch
):
    """The resolution assertions must not depend on the enclosing checkout's ref state.

    This pins the fix for bug b82c-7461-0d95-44b8. The corruption is injected at the layer
    BELOW the stub: ``_advisory_lock.read_pause`` loads ``_ref_lock`` lazily and converts
    ``RefLockCorruptError`` into ``ReconcileGateError``, which is what made ``main()`` return
    early in CI. With ``read_pause`` stubbed, that layer is never reached, so a corrupt
    ambient gate ref cannot reach ``main()``. Without the stub this test fails with the
    original message, so the guard is not bought by going blind.
    """
    monkeypatch.delenv("REBAR_ROOT", raising=False)

    class _RefLockCorruptError(Exception):
        pass

    corrupt_ref_lock = types.SimpleNamespace(
        RefLockCorruptError=_RefLockCorruptError,
        RefLockTimeoutError=_RefLockCorruptError,
        read_pause=MagicMock(side_effect=_RefLockCorruptError("refs/reconciler/gate is corrupt")),
    )

    stack, check_pass_lock_mock, _read_pause_mock = _stub_main_advisory(
        guarded_mod,
        extra_patches=[
            patch(
                f"{_ADVISORY_LOCK_KEY}._load_ref_lock",
                return_value=corrupt_ref_lock,
            )
        ],
    )
    with stack:
        main_mod.main(["--mode=dry-run"])

    assert check_pass_lock_mock.call_count >= 1, (
        "a corrupt ambient refs/reconciler/gate must not stop main() reaching the preflight; "
        "read_pause is stubbed precisely so this test cannot read the enclosing repo's refs"
    )
    _assert_resolved_repo_root(check_pass_lock_mock.call_args)
    assert corrupt_ref_lock.read_pause.call_count == 0, (
        "the stub was bypassed: main() reached the real ref-lock layer, so this test still "
        "depends on the enclosing checkout's refs"
    )


def test_main_without_repo_root_passes_resolved_repo_root(main_mod, guarded_mod, monkeypatch):
    """The Path passed to check_pass_lock when --repo-root is omitted must contain
    the rebar.reconciler package, confirming it resolves to the actual project root.

    The conftest sandbox sets REBAR_ROOT for isolation; clear it
    here so the genuine depth-fallback runs. reconcile_once is mocked, so nothing
    is written to the resolved root.

    This pins the default-resolution path: Path(__file__).resolve().parents[4]
    from __main__.py should reach the repo root, which contains
    src/rebar/reconciler/__main__.py.

    Every advisory call main() reaches is stubbed, read_pause included — see
    _stub_main_advisory for why the ambient gate ref must not reach this test.
    """
    monkeypatch.delenv("REBAR_ROOT", raising=False)

    stack, check_pass_lock_mock, read_pause_mock = _stub_main_advisory(guarded_mod)
    with stack:
        main_mod.main(["--mode=dry-run"])

    assert check_pass_lock_mock.call_count >= 1, "check_pass_lock must be called when main() runs"
    resolved = _assert_resolved_repo_root(check_pass_lock_mock.call_args)
    # The pause read happens FIRST, so pin the resolution there too: this is the call that
    # used to reach the enclosing checkout's refs.
    assert read_pause_mock.call_count >= 1, "read_pause must be called before the preflight"
    assert read_pause_mock.call_args[0][0] == resolved, (
        "read_pause and check_pass_lock must receive the SAME resolved repo root; got "
        f"{read_pause_mock.call_args[0][0]!r} vs {resolved!r}"
    )
