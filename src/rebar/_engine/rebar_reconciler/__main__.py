#!/usr/bin/env python3
"""rebar_reconciler.__main__ — steady-state pass orchestrator.

Invoked as ``python -m rebar_reconciler`` by the GHA reconcile-bridge workflow.
Orchestrates one steady-state pass calling the pipeline modules in sequence:
  fetcher → differ → applier → mapping → manifest → health

Pipeline modules are loaded on demand via ``_try_load_step``; modules that
are not present in this deployment are skipped (graceful no-op), allowing
the orchestrator to be deployed alongside partial module rollouts.

Exit codes:
  0 — all present modules converged successfully
  1 — an unrecoverable error occurred in a pipeline step
"""

from __future__ import annotations

import datetime
import importlib
import importlib.util
import json
import sys
from collections.abc import Callable
from enum import Enum
from functools import partial
from pathlib import Path
from typing import NamedTuple

from rebar_reconciler._heartbeat import Heartbeat as _Heartbeat

# The pass-lock lifecycle cluster, extracted to a sibling module (module-size cap).
# Imported INTO this namespace on purpose: main() calls these as module globals, so
# the suite's patch.object(main_mod, …) targets keep resolving to what main() reads.
from rebar_reconciler._pass_lock_lifecycle import (
    _acquire_or_adopt_pass_lock,
    _lock_steal_enabled,
    _purge_committed_reconciler_locks,
    _resolve_held_lock,
)

# Defensive rebar bootstrap (Tier E E5b): the reconciler now imports the
# in-package ``rebar.*`` store/reducer at runtime. The supported launchers use
# ``sys.executable``, so ``rebar`` is already importable there. This fallback covers
# a bare ``python -m rebar_reconciler`` launched with only the engine dir on PYTHONPATH
# (the historic GHA shape): this file lives at
# <site>/rebar/_engine/rebar_reconciler/__main__.py,
# so parents[3] is the dir containing the ``rebar`` package.
try:
    import rebar  # noqa: F401
except ImportError:  # pragma: no cover - bare-interpreter fallback
    # parents[3] is the PACKAGE PARENT prepended to sys.path (install-location relative by
    # pkg-root-ok: design, correct under editable and wheel installs) — not a checkout root.
    _pkg_parent = str(Path(__file__).resolve().parents[3])
    if _pkg_parent not in sys.path:
        sys.path.insert(0, _pkg_parent)

# Dotted-name keys used for sys.modules seeding so that both production code
# and unit tests (which pre-seed sys.modules with these exact keys) share the
# same module objects and patch() targets resolve correctly.
_ADVISORY_LOCK_KEY = "rebar_reconciler._advisory_lock"
_MODE_KEY = "rebar_reconciler.mode"
_REQUEST_KEY = "rebar_reconciler.request"
# resolve_selection/SelectionError moved to pass_support.py (ticket
# piscine-bullish-cowbird, reconcile_helpers.py module-size headroom).
_PASS_SUPPORT_KEY = "rebar_reconciler.pass_support"


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


def _finish_disposition(
    disposition: _Disposition,
    route: str | None,
    *,
    legacy_message: str | None = None,
    canonical_message: str | None = None,
) -> int:
    """Render one classified result through the canonical or compatibility adapter."""
    if route in {"preview", "sync"}:
        if disposition.canonical_exit == 0:
            print(canonical_message or f"BRIDGE_STATE: {disposition.state}", file=sys.stderr)
        elif canonical_message is not None:
            print(canonical_message, file=sys.stderr)
        elif legacy_message is not None:
            print(legacy_message, file=sys.stderr)
        return disposition.canonical_exit
    if legacy_message is not None:
        print(legacy_message, file=sys.stderr)
    return disposition.legacy_exit


def _load_sibling_keyed(dotted_key: str, filename: str):
    """Load a sibling .py file under *dotted_key* in sys.modules.

    If *dotted_key* is already present in sys.modules, returns the cached
    module — this allows tests to pre-seed the module and have production code
    reuse it, making patch() targets on *dotted_key* work correctly.

    Unlike ``_try_load_step``, this helper raises ``ImportError`` when the
    file is absent rather than returning None, since callers depend on it.
    """
    if dotted_key in sys.modules:
        return sys.modules[dotted_key]
    here = Path(__file__).parent
    path = here / filename
    if not path.exists():
        raise ImportError(f"Required sibling module not found: {path}")
    spec = importlib.util.spec_from_file_location(dotted_key, path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot create spec for {path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[dotted_key] = mod
    spec.loader.exec_module(mod)
    return mod


def _try_load_step(name: str):
    """Load a sibling under its dotted module key, or return ``None`` when absent.

    Register it in ``sys.modules`` before execution so decorators such as
    ``@dataclass`` can resolve the executing module.
    """
    here = Path(__file__).parent
    module_path = here / f"{name}.py"
    if not module_path.exists():
        return None
    dotted_name = f"rebar_reconciler.{name}"
    spec = importlib.util.spec_from_file_location(dotted_name, module_path)
    if spec is None or spec.loader is None:
        return None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[dotted_name] = mod
    spec.loader.exec_module(mod)
    return mod


def _pause_exit_code(advisory, target_mode, mode_mod, repo_root: Path, route: str | None):
    """Return the pause outcome before any reconciler mutation, if applicable."""
    if not hasattr(advisory, "read_pause"):
        return None
    try:
        pause = advisory.read_pause(repo_root)
    except advisory.ReconcileGateError:
        return _finish_disposition(
            _Disposition.OPERATIONAL_FAILURE,
            route,
            legacy_message=(
                "ERROR: refs/reconciler/gate is corrupt; run 'rebar bridge resume' to clear it"
            ),
        )
    if pause is None or mode_mod.MODE_CAPS[target_mode] == 0:
        return None
    status = {
        "paused": True,
        "reason": pause["reason"],
        "who": pause["who"],
        "paused_at": pause["paused_at"],
    }
    marker = f"BRIDGE_PAUSED: {json.dumps(status, separators=(',', ':'))}"
    return _finish_disposition(
        _Disposition.PAUSED,
        route,
        legacy_message=marker,
        canonical_message=marker,
    )


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


def run_pass(
    repo_root: Path | None = None,
    pass_id: str | None = None,
    target_mode=None,
    filter_local_ids: set[str] | None = None,
    selection_kind: str | None = None,
    selection_ids: set[str] | None = None,
    max_changes: int | None = None,
    route: str | None = None,
    abort_check=None,
) -> int:
    """Execute one steady-state reconciliation pass via reconcile.reconcile_once().

    The pass is classified once, then its route translates that disposition:
    canonical preview/sync use 0/1/2 while the compatibility route preserves
    historical 3/4/75 sentinels.

    A missing *pass_id* retains the legacy helper behavior of generating one.
    """
    result = run_pass_result(
        repo_root=repo_root,
        pass_id=pass_id,
        target_mode=target_mode,
        filter_local_ids=filter_local_ids,
        selection_kind=selection_kind,
        selection_ids=selection_ids,
        max_changes=max_changes,
        route=route,
        abort_check=abort_check,
    )
    details = result.details
    if details.get("no_write"):
        if route not in {"preview", "sync"}:
            print(
                f"OK: dry-run computed {details.get('mutation_count', 0)} mutations "
                "(0 applied, no writes)",
                file=sys.stderr,
            )
        print(json.dumps(details))
    elif route not in {"preview", "sync"} and details and result.legacy_message is None:
        computed = details.get("mutation_count", 0)
        applied = details.get("mutations_applied", computed)
        failures = details.get("mutation_failures", 0)
        if computed == 0 and applied == 0:
            print("OK: steady-state pass converged — 0 mutations")
        elif failures == 0:
            print(f"OK: applied {applied} of {computed} mutations")
        else:
            print(f"OK: applied {applied} of {computed} mutations ({failures} failed)")
    return _finish_disposition(
        result.disposition,
        route,
        legacy_message=result.legacy_message,
        canonical_message=result.canonical_message,
    )


def _project_visibility_preflight(repo_root: Path, target_mode, route: str | None):
    """Translate the sibling preflight's ``PreflightAbort`` into ``PassResult``.

    The sibling-keyed loader permits pre-seeded test modules.
    """
    preflight = _load_sibling_keyed("rebar_reconciler._preflight", "_preflight.py")
    abort = preflight.project_visibility_preflight(repo_root, target_mode, route)
    if abort is None:
        return None
    return PassResult(
        _Disposition.OPERATIONAL_FAILURE,
        dict(abort.details),
        canonical_message=abort.message,
        legacy_message=abort.message,
    )


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
    """Execute and classify one pass, returning its structured detail in-process."""
    if repo_root is None:
        from rebar.config import reconciler_repo_root as _owned_repo_root

        repo_root = _owned_repo_root()

    reconcile = _try_load_step("reconcile")
    if reconcile is None:
        if route not in {"preview", "sync"}:
            print("OK: no-op (reconcile.py not present in this deployment)")
        return PassResult(_Disposition.CONVERGED, {})

    # Preflight (ticket a011): verify every mapped project + legacy_default is
    # visible to the backend BEFORE reconcile_once — the only outbound-mutation
    # call. A missing/invisible key (or an unreachable backend) aborts the pass
    # here rather than crashing deep in fan-out (the 05b8 incident class).
    preflight_abort = _project_visibility_preflight(repo_root, target_mode, route)
    if preflight_abort is not None:
        return preflight_abort

    applier = _try_load_step("applier")

    if pass_id is None:
        pass_id = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")
    reschedule_error_cls = getattr(applier, "RescheduleError", None) if applier else None
    try:
        _advisory = (
            None
            if route == "preview"
            else _load_sibling_keyed(_ADVISORY_LOCK_KEY, "_advisory_lock.py")
        )
    except ImportError:
        _advisory = None
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


def _resolve_request_selection(request) -> tuple[set[str] | None, str | None]:
    """Resolve canonical selection tokens before any pass-lock inspection."""
    if not request.selection_tokens:
        return None, None
    helpers = _load_sibling_keyed(_PASS_SUPPORT_KEY, "pass_support.py")
    try:
        return helpers.resolve_selection(request.repo_root, request.selection_tokens), None
    except helpers.SelectionError as exc:
        return None, str(exc)


def _dry_run_enumeration_exit(request) -> int | None:
    """Handle the compatibility directory-enumeration route before lock checks."""
    if not request.dry_run_enumerate:
        return None
    repo_root = request.repo_root
    from rebar.config import reconciler_repo_root as _owned_repo_root
    from rebar.config import tracker_dir as _resolve_store

    resolved_root = repo_root if repo_root is not None else _owned_repo_root()
    # RESOLVED, not composed: enumerating a hardcoded repo-root directory would list
    # nothing (or the wrong tree) on a deployment whose store has been relocated.
    tickets_dir = _resolve_store(resolved_root)
    if not tickets_dir.is_dir():
        return 0
    for entry in sorted(tickets_dir.iterdir()):
        if not entry.is_dir():
            continue
        if ".scratch" in entry.parts:
            continue
        print(entry)
    return 0


def _run_with_last_pass(
    repo_root: Path,
    pass_id: str,
    target_mode: Enum,
    run: Callable[[], int],
    finalize: Callable[[Path, str, Callable[[], int]], int],
) -> int:
    """Publish process results only for modes that may mutate external state."""
    if getattr(target_mode, "value", None) == "dry-run":
        return run()
    return finalize(repo_root, pass_id, run)


def _bind_operation_snapshot(repo_root):
    """Compose and bind one ``OperationSnapshot`` for the remainder of ``main``.

    Return the entered context manager so the caller retains it through the pass and
    exits it explicitly. Retention prevents premature generator cleanup. Explicit exit
    prevents context leakage between in-process calls. Configuration errors preserve
    fail-open ambient resolution.
    """
    from rebar._operation_config import compose_and_bind_operation_snapshot

    cm = compose_and_bind_operation_snapshot(repo_root=repo_root)
    cm.__enter__()
    return cm


def main(argv: list[str] | None = None) -> int:
    """Entry point for ``python -m rebar_reconciler``.

    Guard sequence (execution order required — reordering breaks dd-2/dd-3/dd-4):
      1. argparse           — parse --mode (default: live) and --repo-root
      2. Mode.from_str      — validate mode string BEFORE any fetcher reference (dd-2)
      3. check_pass_lock    — exit non-zero if another pass is in flight (dd-3)
      4. check_phase_gate   — exit non-zero if gate file blocks this mode (dd-4)
      5. acquire_pass_lock  — claim the lock for this pass
      6. try/finally        — run_pass() with guaranteed release_pass_lock (dd-3)
    """
    # Observability floor: install a stderr handler on the reconciler's own logger
    # root. The reconciler's modules log under the sibling ``rebar_reconciler.*`` root
    # (it is imported top-level), so this is distinct from the ``rebar`` root handler.
    from rebar._logging import install_stderr_handler

    install_stderr_handler("rebar_reconciler")

    mode_mod = _load_sibling_keyed(_MODE_KEY, "mode.py")
    request_mod = _load_sibling_keyed(_REQUEST_KEY, "request.py")
    try:
        request = request_mod.normalize_request(argv, mode_mod)
    except (request_mod.RequestError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    repo_root = request.repo_root
    route = getattr(request, "route", None)

    # Bind one composed operation snapshot through the pass and release it in the
    # finalizer.
    operation_snapshot_cm = _bind_operation_snapshot(repo_root)
    try:
        # This compatibility path remains before mode and advisory-lock checks.
        enumeration_exit = _dry_run_enumeration_exit(request)
        if enumeration_exit is not None:
            return enumeration_exit

        target_mode = request.target_mode

        selection_ids, selection_error = _resolve_request_selection(request)
        if selection_error is not None:
            print(f"ERROR: {selection_error}", file=sys.stderr)
            return 2
        if route == "preview":
            return run_pass(
                repo_root=repo_root,
                target_mode=target_mode,
                selection_kind=request.selection_kind,
                selection_ids=selection_ids,
                route=route,
            )

        # Step 2: Advisory lock + phase-gate checks.
        # Load _advisory_lock under the dotted key so tests can pre-seed sys.modules.
        advisory = _load_sibling_keyed(_ADVISORY_LOCK_KEY, "_advisory_lock.py")

        pause_exit = _pause_exit_code(advisory, target_mode, mode_mod, repo_root, route)
        if pause_exit is not None:
            return pause_exit

        # Remove legacy ``.reconciler-*`` ticket-branch locks once. Log Git failures
        # and continue the pass.
        if selection_ids is None:
            _purge_committed_reconciler_locks(repo_root)

        # Use one pass ID as both the lease holder and the run record identifier.
        pass_id = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")

        # Steal an expired pass lock when ``REBAR_RECONCILER_LOCK_STEAL`` permits it.
        # A held lock otherwise exits with status 3.
        held, preflight_exit = _post_pause_preflight(advisory, target_mode, repo_root)
        if preflight_exit is not None:
            legacy_message = (
                "reconcile: refs/reconciler/lock is held — another pass in flight"
                if preflight_exit is _Disposition.IN_FLIGHT
                else (
                    "reconcile: refs/reconciler/gate blocks advancement to "
                    f"{target_mode.value}; clear the gate to advance"
                )
            )
            return _finish_disposition(preflight_exit, route, legacy_message=legacy_message)

        # Resolve a held lock via steal (only reached when steal is enabled — the
        # kill-switch early-returns above). Cases 1/2/3a/3b live in _resolve_held_lock;
        # on the freed fork it acquires via acquire_fn, so the returned oid (stolen or
        # freed-acquired) is adopted below without a second acquire.
        pre_acquired_oid: str | None = None
        if held:
            legacy_exit, pre_acquired_oid, _ok = _resolve_held_lock(
                advisory,
                pass_id,
                repo_root,
                acquire_fn=lambda: advisory.acquire_pass_lock(pass_id, repo_root),
            )
            if legacy_exit is not None:
                return _finish_disposition(
                    _Disposition.IN_FLIGHT,
                    route,
                    legacy_message=(
                        "reconcile: refs/reconciler/lock held by a live pass — yielding"
                    ),
                )

        # Step 3: acquire (or adopt the stolen lock), run pass, release in finally.
        acquired = False
        lock_oid: str | None = None
        heartbeat: _Heartbeat | None = None
        abort_check = None
        from rebar_reconciler import last_pass

        try:
            lock_oid = _acquire_or_adopt_pass_lock(advisory, pass_id, repo_root, pre_acquired_oid)
            acquired = True
            if lock_oid is not None and hasattr(advisory, "_load_ref_lock"):
                ref_lock = advisory._load_ref_lock()
                interval = ref_lock.heartbeat_interval(advisory._lock_lease_secs())
                heartbeat = _Heartbeat(advisory, pass_id, repo_root, lock_oid, interval)
                heartbeat.start()
                _lock_lost = heartbeat.lock_lost

                def abort_check() -> None:
                    if _lock_lost.is_set():
                        raise advisory.ReconcileLockLost(
                            f"pass lock lease lost mid-pass (pass_id={pass_id!r}) — aborting"
                        )

            run_current_pass = partial(
                run_pass,
                repo_root=repo_root,
                pass_id=pass_id,
                target_mode=target_mode,
                filter_local_ids=request.filter_local_ids,
                selection_kind=request.selection_kind,
                selection_ids=selection_ids,
                max_changes=request.max_changes,
                route=route,
                abort_check=abort_check,
            )

            return _run_with_last_pass(
                repo_root,
                pass_id,
                target_mode,
                run_current_pass,
                last_pass.finalize_process,
            )
        except Exception as exc:  # noqa: BLE001 — acquire/heartbeat boundary
            print(f"ERROR: reconciler process failed before finalization: {exc}", file=sys.stderr)
            return 1
        finally:
            last_pass.release_process_lock(advisory, heartbeat, acquired, pass_id, repo_root)
    finally:
        operation_snapshot_cm.__exit__(None, None, None)


if __name__ == "__main__":
    sys.exit(main())
