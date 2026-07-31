"""A lost lease must record WHAT the ref actually holds, not just that it was lost
(bug 4afc-33cc-9e4f-4fe2).

When the heartbeat loses the lease it aborts the whole pass, reporting only:

    ERROR: reconcile heartbeat lost the lease (pass_id=...) — aborting pass

That says the lease is gone and nothing about where it went. "Stolen by whom" is then
unanswerable, which is exactly why the two lease losses on 2026-07-30 (runs
30576272914, 30579382013) cannot be classified as genuine takeovers or spurious
classifications after the fact.

The ref-lock already exposes everything needed: `read()` returns a `RefLockState`
carrying the current `oid`, `holder` and `fence`. Probing it at the moment of loss
turns an unfalsifiable claim into a checkable one — a different holder means a real
takeover, our own oid still on the ref means the CAS failed for some other reason.

The probe is diagnostic only: it must never mask or replace the lease loss, and a probe
that itself fails must degrade to an explicit marker rather than swallowing the abort.
"""

from __future__ import annotations

import types
from pathlib import Path
from typing import Any

import pytest

from rebar_reconciler import __main__ as reconciler_main


class _LeaseLost(Exception):
    pass


def _fake_ref_lock(read_result: Any, *, read_raises: bool = False) -> Any:
    def _read(*a: Any, **k: Any) -> Any:
        if read_raises:
            raise RuntimeError("ls-remote failed")
        return read_result

    return types.SimpleNamespace(
        LeaseLostError=_LeaseLost, LOCK_REF="refs/reconciler/lock", read=_read
    )


def _advisory(ref_lock: Any) -> Any:
    def _renew(*a: Any, **k: Any) -> str:
        raise _LeaseLost("renew CAS rejected — lease lost/stolen")

    return types.SimpleNamespace(
        _load_ref_lock=lambda: ref_lock,
        renew_pass_lock=_renew,
        _lock_remote=lambda *a, **k: "origin",
    )


def _run_heartbeat_once(advisory: Any, tmp_path: Path) -> None:
    hb = reconciler_main._Heartbeat(advisory, "pass-1", tmp_path, "a" * 40, 0.01)
    hb._run()  # first tick renews, raises LeaseLostError, reports, returns
    assert hb.lock_lost.is_set(), "the lease loss must still abort the pass"


def test_lease_loss_reports_who_holds_the_ref_now(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A real takeover must name the new holder, so the claim is checkable."""
    state = types.SimpleNamespace(
        holder="other-pass-2026", lease_secs=120.0, heartbeat_ns=0, fence=7, oid="b" * 40
    )
    _run_heartbeat_once(_advisory(_fake_ref_lock(state)), tmp_path)

    err = capsys.readouterr().err
    assert "lost the lease" in err, err
    assert "other-pass-2026" in err, (
        "the abort must name the holder the ref now carries — without it 'stolen by "
        f"whom' stays unanswerable, which is the whole defect. stderr: {err!r}"
    )
    assert "b" * 40 in err, f"the abort must record the oid the ref now points at. stderr: {err!r}"


def test_unreadable_ref_degrades_explicitly_and_still_aborts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A failing probe must not swallow the abort, and must say it could not read.

    Silence here would be indistinguishable from "nobody holds it", which is precisely
    the ambiguity this ticket exists to remove.
    """
    _run_heartbeat_once(_advisory(_fake_ref_lock(None, read_raises=True)), tmp_path)

    err = capsys.readouterr().err
    assert "lost the lease" in err, "the lease loss must still be reported"
    assert "unreadable" in err.lower() or "unknown" in err.lower(), (
        "a probe that fails must say so explicitly rather than reporting nothing — "
        f"silence reads as 'no holder'. stderr: {err!r}"
    )
