"""Story 9622: crashed-pass lease steal in the held-lock path.

A SIGKILLed pass leaves refs/reconciler/lock held forever; the held-lock path now
attempts steal() (the skew-proof expiry primitive) instead of unconditionally
exiting 3, behind the REBAR_RECONCILER_LOCK_STEAL kill-switch (default ON).

Covers the FIVE held-lock outcomes (via the _resolve_held_lock decision helper and
the _lock_steal_enabled kill-switch), plus the steal_pass_lock wrapper's injected
sleep_fn and fail-closed-on-exception contract.
"""

from __future__ import annotations

import importlib.util
import sys
import types
from pathlib import Path
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
RECON_DIR = REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler"

# Ensure the package shim so __main__/_advisory_lock resolve sibling imports.
if "rebar_reconciler" not in sys.modules:
    _pkg = types.ModuleType("rebar_reconciler")
    _pkg.__path__ = [str(RECON_DIR)]
    sys.modules["rebar_reconciler"] = _pkg


def _load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, RECON_DIR / filename)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def main_mod():
    return _load("reconciler_main_locksteal", "__main__.py")


@pytest.fixture(scope="module")
def advisory_mod():
    return _load("advisory_lock_locksteal", "_advisory_lock.py")


def _fake_advisory(advisory_mod):
    adv = MagicMock()
    adv.ReconcileLockError = advisory_mod.ReconcileLockError
    return adv


REPO = Path("/tmp/repo")


# --------------------------------------------------------------------------- #
# _resolve_held_lock — cases 1, 2, 3a, 3b
# --------------------------------------------------------------------------- #


def test_case1_steal_wins_adopts_new_oid(main_mod, advisory_mod):
    """(1) steal returns a new oid -> adopt it, proceed (acquired), no acquire call."""
    adv = _fake_advisory(advisory_mod)
    adv.steal_pass_lock.return_value = "newoid123"
    acquire_fn = MagicMock()
    exit_code, lock_oid, acquired = main_mod._resolve_held_lock(
        adv, "pass-1", REPO, acquire_fn=acquire_fn
    )
    assert exit_code is None
    assert lock_oid == "newoid123"
    assert acquired is True
    acquire_fn.assert_not_called()  # adopt the stolen oid; don't re-acquire
    adv.check_pass_lock.assert_not_called()  # steal won -> no re-read needed


def test_case2_live_holder_yields_exit3(main_mod, advisory_mod):
    """(2) steal None + ref still held (live holder) -> exit 3."""
    adv = _fake_advisory(advisory_mod)
    adv.steal_pass_lock.return_value = None
    adv.check_pass_lock.return_value = True  # still held by a live holder
    exit_code, lock_oid, acquired = main_mod._resolve_held_lock(
        adv, "pass-2", REPO, acquire_fn=MagicMock()
    )
    assert exit_code == 3
    assert lock_oid is None
    assert acquired is False


def test_case3a_freed_then_acquire_wins_proceeds(main_mod, advisory_mod):
    """(3a) steal None + ref freed + acquire wins -> proceed with the acquired oid."""
    adv = _fake_advisory(advisory_mod)
    adv.steal_pass_lock.return_value = None
    adv.check_pass_lock.return_value = False  # freed during our steal sleep
    acquire_fn = MagicMock(return_value="acqoid456")
    exit_code, lock_oid, acquired = main_mod._resolve_held_lock(
        adv, "pass-3a", REPO, acquire_fn=acquire_fn
    )
    assert exit_code is None
    assert lock_oid == "acqoid456"
    assert acquired is True
    acquire_fn.assert_called_once()


def test_case3b_freed_then_acquire_lost_yields_exit3(main_mod, advisory_mod):
    """(3b) steal None + ref freed + acquire lost to a racer -> exit 3."""
    adv = _fake_advisory(advisory_mod)
    adv.steal_pass_lock.return_value = None
    adv.check_pass_lock.return_value = False  # freed
    acquire_fn = MagicMock(side_effect=advisory_mod.ReconcileLockError("lost race"))
    exit_code, lock_oid, acquired = main_mod._resolve_held_lock(
        adv, "pass-3b", REPO, acquire_fn=acquire_fn
    )
    assert exit_code == 3
    assert lock_oid is None
    assert acquired is False


# --------------------------------------------------------------------------- #
# case 4 — kill-switch
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("value", ["0", "false", "no", "off", "", "  Off  "])
def test_case4_kill_switch_off_disables_steal(main_mod, monkeypatch, value):
    """(4) REBAR_RECONCILER_LOCK_STEAL falsy -> steal disabled (main exits 3 on a held
    lock without stealing)."""
    monkeypatch.setenv("REBAR_RECONCILER_LOCK_STEAL", value)
    assert main_mod._lock_steal_enabled() is False


@pytest.mark.parametrize("value", ["1", "true", "yes", "on", "anything"])
def test_kill_switch_on_by_default(main_mod, monkeypatch, value):
    monkeypatch.setenv("REBAR_RECONCILER_LOCK_STEAL", value)
    assert main_mod._lock_steal_enabled() is True


def test_kill_switch_default_on_when_unset(main_mod, monkeypatch):
    monkeypatch.delenv("REBAR_RECONCILER_LOCK_STEAL", raising=False)
    assert main_mod._lock_steal_enabled() is True


# --------------------------------------------------------------------------- #
# steal_pass_lock wrapper — injected sleep_fn + fail-closed
# --------------------------------------------------------------------------- #


def test_steal_pass_lock_passes_sleep_fn_and_returns_oid(advisory_mod, monkeypatch):
    """The wrapper delegates to _ref_lock.steal with the injected sleep_fn and
    returns its result (a new oid)."""
    fake_ref_lock = MagicMock()
    fake_ref_lock.LOCK_REF = "refs/reconciler/lock"
    fake_ref_lock.steal.return_value = "stolen-oid"
    monkeypatch.setattr(advisory_mod, "_load_ref_lock", lambda: fake_ref_lock)

    sleep_fn = MagicMock()
    result = advisory_mod.steal_pass_lock("pass-x", REPO, sleep_fn=sleep_fn)

    assert result == "stolen-oid"
    _, kwargs = fake_ref_lock.steal.call_args
    assert kwargs["holder"] == "pass-x"
    assert kwargs["sleep_fn"] is sleep_fn


def test_steal_pass_lock_fail_closed_on_exception(advisory_mod, monkeypatch):
    """A git transport/permission error from steal() is caught and reported as
    'not stolen' (None) — never crashes the pass."""
    fake_ref_lock = MagicMock()
    fake_ref_lock.LOCK_REF = "refs/reconciler/lock"
    fake_ref_lock.steal.side_effect = RuntimeError("git transport blew up")
    monkeypatch.setattr(advisory_mod, "_load_ref_lock", lambda: fake_ref_lock)

    result = advisory_mod.steal_pass_lock("pass-y", REPO, sleep_fn=MagicMock())
    assert result is None
