"""Lease-push rejection classification contracts.

Only a moved force-with-lease target is a CAS mismatch. Server, hook, quota,
and transport failures remain fail-closed. CAS verdicts retain Git stderr on
the established ``rebar_reconciler._ref_lock`` logger. Moved helpers preserve
their original patch points and exports.
"""

from __future__ import annotations

import logging
import subprocess
import types
from typing import Any

import pytest

from rebar_reconciler import _ref_lock

REF = "refs/reconciler/lock"
OLD = "0" * 40

# Real git stderr shapes. Only the first is a lease mismatch.
CAS_MISMATCH = "error: cannot lock ref: is at abc but expected def\n! [rejected] (stale info)"
NON_CAS = {
    "server-side ref contention": (
        "error: cannot lock ref 'refs/reconciler/lock': "
        "Unable to create '.../refs/reconciler/lock.lock': File exists."
    ),
    "remote internal error": (
        "! [remote rejected] refs/reconciler/lock -> refs/reconciler/lock (internal server error)"
    ),
    "pre-receive hook declined": (
        "! [remote rejected] refs/reconciler/lock (pre-receive hook declined)"
    ),
    # A server-side witness failure with an unchanged lease stays fail-closed.
    "github ref-transaction failure": (
        "remote: fatal error in commit_refs\n"
        "! [remote rejected]       1e5caeb7c8c9af4ab7cb33501eae7102ba3efa47 -> "
        "refs/reconciler/last-pass (failure)"
    ),
    "secondary rate limit": (
        "! [remote rejected] refs/reconciler/lock (You have exceeded a secondary rate limit)"
    ),
}
TRANSPORT = {
    "auth failed": "fatal: Authentication failed for 'https://github.com/navapbc/rebar'",
    "dns failure": "fatal: Could not resolve host: github.com",
}


def _run_with_stderr(monkeypatch: pytest.MonkeyPatch, stderr: str) -> None:
    """Drive _push_lease_cas with a git that fails carrying *stderr*."""

    def _git(*a: Any, **k: Any) -> Any:
        return types.SimpleNamespace(returncode=1, stderr=stderr, stdout="", args=["git", "push"])

    monkeypatch.setattr(_ref_lock, "_git", _git)
    _ref_lock._push_lease_cas(None, REF, OLD, "origin", f"x:{REF}")


def _classify(monkeypatch: pytest.MonkeyPatch, stderr: str) -> str:
    try:
        _run_with_stderr(monkeypatch, stderr)
    except subprocess.CalledProcessError as exc:
        return "cas-mismatch" if exc.returncode == 128 else "fail-closed"
    except Exception:  # noqa: BLE001 - any other raise is still fail-closed
        return "fail-closed"
    return "success"


def test_genuine_lease_mismatch_is_still_a_cas_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The real `--force-with-lease` rejection must keep its current classification."""
    assert _classify(monkeypatch, CAS_MISMATCH) == "cas-mismatch"


@pytest.mark.parametrize("label", sorted(NON_CAS))
def test_non_cas_rejections_are_not_reported_as_a_stolen_lease(
    label: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A rejection with an unchanged lease remains a fail-closed transport error."""
    verdict = _classify(monkeypatch, NON_CAS[label])
    assert verdict == "fail-closed", (
        f"{label!r} is not a lease mismatch — the lease never moved — so it must "
        f"fail closed rather than be reported as a stolen lease. got {verdict!r}"
    )


@pytest.mark.parametrize("label", sorted(TRANSPORT))
def test_transport_failures_stay_fail_closed(label: str, monkeypatch: pytest.MonkeyPatch) -> None:
    """Contrast case: the existing fail-closed behaviour must not regress."""
    assert _classify(monkeypatch, TRANSPORT[label]) == "fail-closed", label


def test_cas_mismatch_logs_the_stderr_that_justified_it(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """A CAS verdict logs the Git stderr, ref, and expected object identifier."""
    with caplog.at_level(logging.WARNING):
        verdict = _classify(monkeypatch, CAS_MISMATCH)
    assert verdict == "cas-mismatch"
    logged = "\n".join(r.getMessage() for r in caplog.records)
    assert "stale info" in logged, (
        "the CAS-mismatch branch must log the git stderr that justified classifying the "
        f"push as a lost lease, so a production occurrence is diagnosable. logged: {logged!r}"
    )
    assert REF in logged, f"the log must name the ref. logged: {logged!r}"
    assert OLD in logged, (
        "the log must name the EXPECTED oid — it is the value you compare against what "
        "the ref actually holds, which is the whole point of making the claim checkable. "
        f"logged: {logged!r}"
    )


def test_a_github_server_side_ref_failure_is_not_reported_as_a_stolen_lease(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A server-side witness failure with an intact lease remains fail-closed."""
    assert _classify(monkeypatch, NON_CAS["github ref-transaction failure"]) == "fail-closed"


def test_stale_info_still_wins_over_the_new_server_side_markers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`stale info` stays CONCLUSIVE — a real lease move is still detected (bug 4afc)."""
    combined = (
        "remote: fatal error in commit_refs\n! [rejected] refs/reconciler/last-pass (stale info)"
    )
    assert _classify(monkeypatch, combined) == "cas-mismatch"


# The push-helper split preserves the original patch point, logger, and exports.


@pytest.mark.parametrize(
    "call",
    [
        pytest.param(
            lambda: _ref_lock._push_lease_cas(None, REF, OLD, "origin", f"x:{REF}"),
            id="_push_lease_cas",
        ),
        pytest.param(
            lambda: _ref_lock._push_cas(None, REF, "y" * 40, OLD, "origin"), id="_push_cas"
        ),
        pytest.param(
            lambda: _ref_lock._push_delete_cas(None, REF, OLD, "origin"), id="_push_delete_cas"
        ),
    ],
)
def test_the_git_patch_point_survives_the_module_split(
    call: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Lease pushes resolve ``_git`` from ``_ref_lock`` at call time.

    This preserves the existing monkeypatch seam after the helper split.
    """
    calls: list[Any] = []

    def _git(*a: Any, **k: Any) -> Any:
        calls.append((a, k))
        return types.SimpleNamespace(returncode=0, stderr="", stdout="", args=["git", "push"])

    monkeypatch.setattr(_ref_lock, "_git", _git)
    call()
    assert calls, (
        "the patched _ref_lock._git was never called — the split moved the lease push out "
        "of reach of the patch point the existing tests rely on"
    )
    assert calls[0][0][1][0] == "push", calls


def test_the_cas_verdict_still_logs_on_the_ref_lock_logger(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """CAS evidence remains on the ``rebar_reconciler._ref_lock`` logger."""
    with caplog.at_level(logging.WARNING):
        assert _classify(monkeypatch, CAS_MISMATCH) == "cas-mismatch"
    cas_records = [r for r in caplog.records if "CAS mismatch" in r.getMessage()]
    assert cas_records, (
        f"no CAS-mismatch warning logged: {[r.getMessage() for r in caplog.records]}"
    )
    assert [r.name for r in cas_records] == ["rebar_reconciler._ref_lock"] * len(cas_records), (
        "the CAS verdict must be emitted on the rebar_reconciler._ref_lock logger, not on "
        f"the sibling module's. got: {[r.name for r in cas_records]}"
    )


@pytest.mark.parametrize(
    "name", ["_push_lease_cas", "_push_cas", "_push_delete_cas", "_is_cas_mismatch_stderr"]
)
def test_moved_names_stay_resolvable_at_the_original_module_path(name: str) -> None:
    """Moved push helpers remain callable from their original module path."""
    assert callable(getattr(_ref_lock, name)), f"{name} is no longer resolvable on _ref_lock"
