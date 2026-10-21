"""The one guarded reconciliation pass shared by the CLI, the library, and MCP.

``run_guarded_pass`` runs the guard sequence that used to live only in
``__main__.main()``: selection resolution, the pause gate, the legacy lock-file purge,
the lock/phase preflight, pass-lock steal or acquire, the lease heartbeat with its
``abort_check``, the last-pass witness, and the release in ``finally``. The CLI
(``python -m rebar.reconciler``) and ``rebar.bridge_preview``/``rebar.bridge_sync``
(and so the MCP ``bridge_*`` tools) all call it, so a library or MCP live pass holds
the same lease and honours the same pause as the command line.

It returns a classified ``PassResult`` and prints nothing to stdout; each route
renders the result itself (``__main__._render_pass`` for the CLI, ``_lib_ops`` for
the library).

``_try_load_step`` and ``_load_sibling_keyed`` are named import seams so tests can
``patch.object`` the pipeline modules this pass loads.
"""

from __future__ import annotations

import datetime
import importlib
import json
from collections.abc import Callable
from enum import Enum
from functools import partial
from pathlib import Path
from typing import NamedTuple

from rebar.reconciler._heartbeat import Heartbeat
from rebar.reconciler._pass_lock_lifecycle import (
    _acquire_or_adopt_pass_lock,
    _lock_steal_enabled,
    _purge_committed_reconciler_locks,
    _resolve_held_lock,
)

_ADVISORY_LOCK_KEY = "rebar.reconciler._advisory_lock"
_MODE_KEY = "rebar.reconciler.mode"
_REQUEST_KEY = "rebar.reconciler.request"
_PASS_SUPPORT_KEY = "rebar.reconciler.pass_support"


class _Disposition(Enum):
    """One semantic pass outcome, translated only at the invoking route."""

    CONVERGED = ("converged", 0, 0)
    PAUSED = ("paused", 0, 0)
    IN_FLIGHT = ("in-flight", 0, 3)
    PHASE_GATE = ("legacy-gated", 0, 4)
    RESCHEDULE = ("reschedule", 0, 75)
    OPERATIONAL_FAILURE = ("operational_failure", 1, 1)
    INVALID_INVOCATION = ("invalid_invocation", 2, 2)

    def __init__(self, state: str, canonical_exit: int, legacy_exit: int):
        self.state = state
        self.canonical_exit = canonical_exit
        self.legacy_exit = legacy_exit


class PassResult(NamedTuple):
    """One classified pass before a route collapses it to a process exit."""

    disposition: _Disposition
    details: dict[str, object]
    legacy_message: str | None = None
    canonical_message: str | None = None


def _load_sibling_keyed(dotted_key: str, filename: str):
    """Import the in-package sibling *dotted_key* (``filename`` is kept for the test seam).

    A named seam rather than an inline import so tests can ``patch.object`` the loader.
    """
    del filename
    return importlib.import_module(dotted_key)


def _try_load_step(name: str):
    """Import the in-package pipeline step ``rebar.reconciler.<name>`` (a test seam)."""
    return importlib.import_module(f"rebar.reconciler.{name}")


def _pause_result(advisory, target_mode, mode_mod, repo_root: Path) -> PassResult | None:
    """Return the pause outcome before any reconciler mutation, if applicable."""
    if not hasattr(advisory, "read_pause"):
        return None
    try:
        pause = advisory.read_pause(repo_root)
    except advisory.ReconcileGateError:
        return PassResult(
            _Disposition.OPERATIONAL_FAILURE,
            {},
            legacy_message=(
                "ERROR: refs/reconciler/gate is corrupt; run 'rebar bridge resume' to clear it"
            ),
        )
    if pause is None or mode_mod.MODE_CAPS[target_mode] == 0:
        return None
    status: dict[str, object] = {
        "paused": True,
        "reason": pause["reason"],
        "who": pause["who"],
        "paused_at": pause["paused_at"],
    }
    marker = f"BRIDGE_PAUSED: {json.dumps(status, separators=(',', ':'))}"
    return PassResult(_Disposition.PAUSED, status, legacy_message=marker, canonical_message=marker)


def _post_pause_preflight(
    advisory, target_mode, repo_root: Path
) -> tuple[bool, _Disposition | None]:
    """Check the pre-existing lock and phase guards after the pause decision."""
    held = advisory.check_pass_lock(repo_root)
    if held and not _lock_steal_enabled():
        return held, _Disposition.IN_FLIGHT
    if advisory.check_phase_gate(target_mode, repo_root):
        return held, _Disposition.PHASE_GATE
    return held, None


def _optional_request_kwargs(
    selection_kind: str | None,
    selection_ids: set[str] | None,
    max_changes: int | None,
) -> dict[str, object]:
    """Return only canonical request fields explicitly supplied by the caller."""
    kwargs: dict[str, object] = {}
    if selection_kind is not None:
        kwargs["selection_kind"] = selection_kind
    if selection_ids is not None:
        kwargs["selection_ids"] = selection_ids
    if max_changes is not None:
        kwargs["max_changes"] = max_changes
    return kwargs


def _reconcile_exception_result(
    exc: Exception,
    *,
    reschedule_error_cls,
    lock_lost_cls,
) -> PassResult:
    """Classify one failed reconcile_once call without choosing an adapter."""
    details: dict[str, object] = {"error": str(exc)}
    if type(exc).__name__ == "SelectionStaleError":
        return PassResult(_Disposition.INVALID_INVOCATION, details, legacy_message=f"ERROR: {exc}")
    # Classify credential rejection as INVALID_INVOCATION by exception name, which
    # avoids importing the Jira adapter.
    if type(exc).__name__ == "AcliAuthError":
        details["error_class"] = "auth_failed"
        message = (
            "ERROR: Jira credential REJECTED — the reconciler is not authenticated. "
            "This is a credential problem, not a data problem: rotate the JIRA_API_TOKEN "
            f"secret (re-running `acli auth login` with the same token cannot help). {exc}"
        )
        return PassResult(_Disposition.INVALID_INVOCATION, details, legacy_message=message)
    if reschedule_error_cls is not None and isinstance(exc, reschedule_error_cls):
        message = f"RESCHEDULE: reconcile_once signalled reschedule: {exc}"
        return PassResult(_Disposition.RESCHEDULE, details, legacy_message=message)
    if lock_lost_cls is not None and isinstance(exc, lock_lost_cls):
        message = f"RESCHEDULE: pass lock lease lost mid-pass: {exc}"
        return PassResult(_Disposition.RESCHEDULE, details, legacy_message=message)
    return PassResult(
        _Disposition.OPERATIONAL_FAILURE,
        details,
        legacy_message=f"ERROR: reconcile_once raised: {exc}",
    )


def _project_visibility_preflight(repo_root: Path, target_mode, route: str | None):
    """Translate the sibling preflight's ``PreflightAbort`` into ``PassResult``.

    The sibling-keyed loader permits pre-seeded test modules.
    """
    preflight = _load_sibling_keyed("rebar.reconciler._preflight", "_preflight.py")
    abort = preflight.project_visibility_preflight(repo_root, target_mode, route)
    if abort is None:
        return None
    return PassResult(
        _Disposition.OPERATIONAL_FAILURE,
        dict(abort.details),
        canonical_message=abort.message,
        legacy_message=abort.message,
    )


def _classify_reconcile_result(result: dict) -> PassResult:
    """Classify a completed ``reconcile_once`` result dict."""
    failures = result.get("mutation_failures", 0)
    if result.get("no_write"):
        return PassResult(_Disposition.CONVERGED, result)
    # Per-mutation failures are operational failures; successful fallbacks are
    # counted as applied by reconcile.py and therefore remain converged.
    if failures > 0:
        return PassResult(
            _Disposition.OPERATIONAL_FAILURE,
            result,
            canonical_message=f"ERROR: reconcile completed with {failures} mutation failures",
        )
    # RP-03 S3 T3: a degraded cutover pass with no failures (its fuse opened, so matching
    # work was safety-deferred rather than converged) must still exit non-zero.
    if result.get("degraded"):
        return PassResult(
            _Disposition.OPERATIONAL_FAILURE,
            result,
            canonical_message="ERROR: reconcile completed degraded (fuse opened)",
        )
    return PassResult(_Disposition.CONVERGED, result)


def run_pass_result(
    repo_root: Path | None = None,
    pass_id: str | None = None,
    target_mode=None,
    filter_local_ids: set[str] | None = None,
    selection_kind: str | None = None,
    selection_ids: set[str] | None = None,
    max_changes: int | None = None,
    route: str | None = None,
    abort_check=None,
) -> PassResult:
    """Execute and classify one pass body, returning its structured detail in-process.

    This is the unguarded body: callers go through :func:`run_guarded_pass`.
    """
    if repo_root is None:
        from rebar.config import reconciler_repo_root as _owned_repo_root

        repo_root = _owned_repo_root()

    reconcile = _try_load_step("reconcile")

    # Preflight (ticket a011): verify every mapped project + legacy_default is
    # visible to the backend BEFORE reconcile_once — the only outbound-mutation
    # call. A missing/invisible key (or an unreachable backend) aborts the pass
    # here rather than crashing deep in fan-out (the 05b8 incident class).
    preflight_abort = _project_visibility_preflight(repo_root, target_mode, route)
    if preflight_abort is not None:
        return preflight_abort

    applier = _try_load_step("applier")

    if pass_id is None:
        pass_id = _new_pass_id()
    reschedule_error_cls = getattr(applier, "RescheduleError", None) if applier else None
    _advisory = (
        None if route == "preview" else _load_sibling_keyed(_ADVISORY_LOCK_KEY, "_advisory_lock.py")
    )
    lock_lost_cls = getattr(_advisory, "ReconcileLockLost", None) if _advisory else None

    try:
        result = reconcile.reconcile_once(
            pass_id,
            repo_root=repo_root,
            target_mode=target_mode,
            filter_local_ids=filter_local_ids,
            abort_check=abort_check,
            **_optional_request_kwargs(selection_kind, selection_ids, max_changes),
            **({"route": route} if route is not None else {}),
        )
    except Exception as exc:  # noqa: BLE001 — classification owns the process contract
        return _reconcile_exception_result(
            exc,
            reschedule_error_cls=reschedule_error_cls,
            lock_lost_cls=lock_lost_cls,
        )
    return _classify_reconcile_result(result)


def _new_pass_id() -> str:
    return datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")


def _resolve_request_selection(request) -> tuple[set[str] | None, str | None]:
    """Resolve canonical selection tokens before any pass-lock inspection."""
    if not request.selection_tokens:
        return None, None
    helpers = _load_sibling_keyed(_PASS_SUPPORT_KEY, "pass_support.py")
    try:
        return helpers.resolve_selection(request.repo_root, request.selection_tokens), None
    except helpers.SelectionError as exc:
        return None, str(exc)


def _run_with_last_pass(
    repo_root: Path,
    pass_id: str,
    target_mode: Enum,
    run: Callable[[], PassResult],
    finalize: Callable[[Path, str, Callable[[], PassResult]], PassResult],
) -> PassResult:
    """Publish process results only for modes that may mutate external state."""
    if getattr(target_mode, "value", None) == "dry-run":
        return run()
    return finalize(repo_root, pass_id, run)


def _pre_lock_result(
    advisory, request, mode_mod, selection_ids, pass_id: str
) -> tuple[PassResult | None, str | None]:
    """Run the guards that decide before the lease: pause, purge, lock and phase preflight.

    Returns ``(result, adopted_oid)``: a non-``None`` result ends the pass; otherwise
    ``adopted_oid`` is the lock a steal already took (``None`` when the caller acquires).
    """
    target_mode = request.target_mode
    paused = _pause_result(advisory, target_mode, mode_mod, request.repo_root)
    if paused is not None:
        return paused, None
    # Remove legacy ``.reconciler-*`` ticket-branch locks once. Log Git failures and
    # continue the pass.
    if selection_ids is None:
        _purge_committed_reconciler_locks(request.repo_root)
    # Steal an expired pass lock when ``REBAR_RECONCILER_LOCK_STEAL`` permits it; a
    # held lock otherwise yields.
    held, preflight = _post_pause_preflight(advisory, target_mode, request.repo_root)
    if preflight is not None:
        message = (
            "reconcile: refs/reconciler/lock is held — another pass in flight"
            if preflight is _Disposition.IN_FLIGHT
            else (
                "reconcile: refs/reconciler/gate blocks advancement to "
                f"{target_mode.value}; clear the gate to advance"
            )
        )
        return PassResult(preflight, {}, legacy_message=message), None
    if not held:
        return None, None
    legacy_exit, adopted, _ok = _resolve_held_lock(
        advisory,
        pass_id,
        request.repo_root,
        acquire_fn=lambda: advisory.acquire_pass_lock(pass_id, request.repo_root),
    )
    if legacy_exit is not None:
        message = "reconcile: refs/reconciler/lock held by a live pass — yielding"
        return PassResult(_Disposition.IN_FLIGHT, {}, legacy_message=message), None
    return None, adopted


def _start_heartbeat(advisory, pass_id: str, repo_root: Path, lock_oid: str | None):
    """Start the lease heartbeat and return ``(heartbeat, abort_check)``."""
    if lock_oid is None or not hasattr(advisory, "_load_ref_lock"):
        return None, None
    ref_lock = advisory._load_ref_lock()
    interval = ref_lock.heartbeat_interval(advisory._lock_lease_secs())
    heartbeat = Heartbeat(advisory, pass_id, repo_root, lock_oid, interval)
    heartbeat.start()
    lock_lost = heartbeat.lock_lost

    def abort_check() -> None:
        if lock_lost.is_set():
            raise advisory.ReconcileLockLost(
                f"pass lock lease lost mid-pass (pass_id={pass_id!r}) — aborting"
            )

    return heartbeat, abort_check


def _run_locked(
    advisory, request, run_body: Callable[..., PassResult], pass_id: str, adopted: str | None
) -> PassResult:
    """Acquire (or adopt) the lease, run the pass under the witness, release in ``finally``."""
    from rebar.reconciler import last_pass

    acquired = False
    heartbeat = None
    try:
        lock_oid = _acquire_or_adopt_pass_lock(advisory, pass_id, request.repo_root, adopted)
        acquired = True
        heartbeat, abort_check = _start_heartbeat(advisory, pass_id, request.repo_root, lock_oid)

        def run_current_pass() -> PassResult:
            return run_body(pass_id=pass_id, abort_check=abort_check)

        return _run_with_last_pass(
            request.repo_root,
            pass_id,
            request.target_mode,
            run_current_pass,
            partial(last_pass.finalize_process, route=request.route),
        )
    except Exception as exc:  # noqa: BLE001 — acquire/heartbeat boundary
        return PassResult(
            _Disposition.OPERATIONAL_FAILURE,
            {"error": str(exc)},
            legacy_message=f"ERROR: reconciler process failed before finalization: {exc}",
        )
    finally:
        last_pass.release_process_lock(advisory, heartbeat, acquired, pass_id, request.repo_root)


def run_guarded_pass(request, *, advisory=None) -> PassResult:
    """Run one reconciliation pass behind the full guard sequence.

    ``request`` is a ``rebar.reconciler.request.ReconcileRequest``. The order is fixed:
    selection resolution (every route), then ``preview`` runs unguarded (it never
    mutates), then the pause gate, the lock-file purge, the lock and phase preflight,
    the steal-or-acquire, and the pass itself under the heartbeat and the last-pass
    witness, releasing the lease in ``finally``. ``advisory`` overrides the advisory
    lock module (tests); by default it is loaded through ``_load_sibling_keyed``.
    """
    selection_ids, selection_error = _resolve_request_selection(request)
    if selection_error is not None:
        message = f"ERROR: {selection_error}"
        return PassResult(
            _Disposition.INVALID_INVOCATION,
            {"error": selection_error},
            legacy_message=message,
            canonical_message=message,
        )
    run_body = partial(
        run_pass_result,
        repo_root=request.repo_root,
        target_mode=request.target_mode,
        filter_local_ids=request.filter_local_ids,
        selection_kind=request.selection_kind,
        selection_ids=selection_ids,
        max_changes=request.max_changes,
        route=request.route,
    )
    if request.route == "preview":
        return run_body()
    if advisory is None:
        advisory = _load_sibling_keyed(_ADVISORY_LOCK_KEY, "_advisory_lock.py")
    mode_mod = _load_sibling_keyed(_MODE_KEY, "mode.py")
    # One pass ID is both the lease holder and the run record identifier.
    pass_id = _new_pass_id()
    early, adopted = _pre_lock_result(advisory, request, mode_mod, selection_ids, pass_id)
    if early is not None:
        return early
    return _run_locked(advisory, request, run_body, pass_id, adopted)


__all__ = ["PassResult", "run_guarded_pass", "run_pass_result"]
