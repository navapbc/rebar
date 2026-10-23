"""A mid-pass lease loss aborts reconciliation and exits for rescheduling.

ReconcileLockLost maps to exit 75, which the bridge workflow treats as a
retryable pass. An unrelated failure still exits 1. The lock-loss abort keeps
the no-overlap invariant while allowing the next scheduled run to retry."""

from __future__ import annotations

import types
from pathlib import Path
from typing import Any

import pytest

from rebar.reconciler import __main__ as reconciler_main
from rebar.reconciler import guarded_pass


def _lock_lost_cls() -> type[Exception]:
    """Resolve the production ReconcileLockLost class so the test raises its exact exception."""
    advisory = guarded_pass._load_sibling_keyed(
        guarded_pass._ADVISORY_LOCK_KEY, "_advisory_lock.py"
    )
    return advisory.ReconcileLockLost  # type: ignore[no-any-return]


def _install_steps(monkeypatch: pytest.MonkeyPatch, reconcile_once_exc: BaseException) -> None:
    """Stub `_try_load_step` so reconcile_once raises, with a real applier contract."""

    class _RescheduleError(Exception):
        pass

    applier_stub = types.SimpleNamespace(RescheduleError=_RescheduleError, EXIT_RESCHEDULE=75)

    def _reconcile_once(*args: Any, **kwargs: Any) -> Any:
        raise reconcile_once_exc

    reconcile_stub = types.SimpleNamespace(reconcile_once=_reconcile_once)

    def _fake_load(name: str) -> Any:
        return {"reconcile": reconcile_stub, "applier": applier_stub}.get(name)

    monkeypatch.setattr(guarded_pass, "_try_load_step", _fake_load)


def test_lock_lost_mid_pass_exits_reschedule_not_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A lost mid-pass lease reports rescheduling and exits 75 rather than 1."""
    _install_steps(
        monkeypatch,
        _lock_lost_cls()(
            "pass lock lease lost mid-pass (pass_id='2026-07-30T19-50-55') — aborting"
        ),
    )

    rc = reconciler_main._render_pass(
        guarded_pass.run_pass_result(repo_root=tmp_path, pass_id="2026-07-30T19-50-55"), None
    )

    assert rc == 75, (
        "losing the pass lock mid-pass is benign contention — the same class of event "
        "that returns 3 when detected pre-pass — so it must return the reschedule code "
        f"the workflow already treats as non-fatal, not {rc}"
    )
    err = capsys.readouterr().err
    assert "lease lost mid-pass" in err, (
        "the benign exit must still name the lease loss on stderr — benign is not "
        f"silent. stderr was: {err!r}"
    )


def test_generic_failure_still_exits_one(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Only lock loss is reclassified. An unrelated pass failure still exits 1."""
    _install_steps(monkeypatch, RuntimeError("genuine unrecoverable failure"))

    rc = reconciler_main._render_pass(
        guarded_pass.run_pass_result(repo_root=tmp_path, pass_id="test-pass"), None
    )

    assert rc == 1, f"a genuine failure must still be fatal (exit 1), got {rc}"
