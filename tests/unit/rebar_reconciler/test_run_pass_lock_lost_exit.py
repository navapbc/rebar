"""Losing the pass lock mid-pass is benign contention, not an unrecoverable error
(bug 449f-f9bf-be90-47fe, mode 1).

The reconciler already treats "another pass holds the lock" as benign when it is
detected BEFORE the pass starts: `main` returns 3, and the workflow prints
"Another reconcile pass is in flight (exit 3) — benign, skipping".

Mid-pass the same event is classified as fatal. The heartbeat sets `lock_lost`, the
`abort_check` closure raises `ReconcileLockLost` — which is a bare `RuntimeError`
subclass, NOT `applier.RescheduleError` — so `run_pass`'s `except Exception` falls
past the reschedule arm to `return 1`. `.github/workflows/reconcile-bridge.yml`
whitelists only 0/75/3, so exit 1 hits the `*)` arm and the run goes red.

Aborting the pass is correct and deliberate (ADR 0031; ticket 2711: "heartbeat loses
lease mid-pass -> pass aborts, no double-run"). What was never specified is the exit
code, and ADR 0031 states the property that decides it: "the pass aborts, the finally
release no-ops, **a re-run is idempotent**" — which is precisely the meaning of
EXIT_RESCHEDULE (75), documented in the workflow as "next scheduled run will retry".

Observed in production: runs 30576272914 (2026-07-30T19:52Z) and 30579382013
(20:38Z), both `##[error]Reconcile failed (exit 1)` after a lost lease.
"""

from __future__ import annotations

import types
from pathlib import Path
from typing import Any

import pytest

from rebar_reconciler import __main__ as reconciler_main


def _lock_lost_cls() -> type[Exception]:
    """Resolve ReconcileLockLost exactly as production does.

    Both the raise site (`main`) and the classification site (`run_pass`) go through
    `_load_sibling_keyed(_ADVISORY_LOCK_KEY, ...)`, which deliberately returns a
    test-pre-seeded `sys.modules` entry when one exists. Importing
    `rebar_reconciler._advisory_lock` directly instead can therefore hand back a
    DIFFERENT class object once another test in the session has seeded a stub under
    that key — `isinstance` then fails and this test goes red for a reason that has
    nothing to do with the behaviour under test (the order-dependent class-identity
    hazard tracked by bug 9f0b). Resolving through the same loader keeps the test
    faithful to the production path and immune to that ordering.
    """
    advisory = reconciler_main._load_sibling_keyed(
        reconciler_main._ADVISORY_LOCK_KEY, "_advisory_lock.py"
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

    monkeypatch.setattr(reconciler_main, "_try_load_step", _fake_load)


def test_lock_lost_mid_pass_exits_reschedule_not_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A lost lease returns the benign reschedule code, and says so on stderr.

    Exit 1 is what turns a benign lock handover into a red CI run. 75 is already
    wired as "next scheduled run will retry", which is exactly ADR 0031's
    "a re-run is idempotent".
    """
    _install_steps(
        monkeypatch,
        _lock_lost_cls()(
            "pass lock lease lost mid-pass (pass_id='2026-07-30T19-50-55') — aborting"
        ),
    )

    rc = reconciler_main.run_pass(repo_root=tmp_path, pass_id="2026-07-30T19-50-55")

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
    """Contrast case: only the lock-lost signal is reclassified.

    Guards against the fix degenerating into "treat every reconcile_once failure as
    benign", which would hide real faults (the mode-3 404 among them).
    """
    _install_steps(monkeypatch, RuntimeError("genuine unrecoverable failure"))

    rc = reconciler_main.run_pass(repo_root=tmp_path, pass_id="test-pass")

    assert rc == 1, f"a genuine failure must still be fatal (exit 1), got {rc}"
