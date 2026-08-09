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
import os
import sys
import threading
from enum import Enum
from pathlib import Path

# Defensive rebar bootstrap (Tier E E5b): the reconciler now imports the
# in-package ``rebar.*`` store/reducer at runtime. The supported launchers
# (`rebar reconcile` / `rebar.reconcile()`) use ``sys.executable``, so ``rebar``
# is already importable there. This fallback covers a bare ``python -m
# rebar_reconciler`` launched with only the engine dir on PYTHONPATH (the historic
# GHA shape): this file lives at <site>/rebar/_engine/rebar_reconciler/__main__.py,
# so parents[3] is the dir containing the ``rebar`` package.
try:
    import rebar  # noqa: F401
except ImportError:  # pragma: no cover - bare-interpreter fallback
    _pkg_parent = str(Path(__file__).resolve().parents[3])
    if _pkg_parent not in sys.path:
        sys.path.insert(0, _pkg_parent)

# Dotted-name keys used for sys.modules seeding so that both production code
# and unit tests (which pre-seed sys.modules with these exact keys) share the
# same module objects and patch() targets resolve correctly.
_ADVISORY_LOCK_KEY = "rebar_reconciler._advisory_lock"
_MODE_KEY = "rebar_reconciler.mode"
_REQUEST_KEY = "rebar_reconciler.request"
_HELPERS_KEY = "rebar_reconciler.reconcile_helpers"


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
    """Attempt to import a sibling module by name; return None if absent.

    Registers the loaded module in ``sys.modules`` under its dotted spec name
    (``rebar_reconciler.<name>``) BEFORE exec_module runs. This is load-bearing
    on Python 3.14 because the new dataclass type-resolution helper
    (``dataclasses._is_type`` -> ``sys.modules.get(cls.__module__).__dict__``)
    requires that any module containing a ``@dataclass`` be discoverable via
    the same key the class's ``__module__`` attribute points at. If
    ``sys.modules.get(cls.__module__)`` returns None (because we loaded the
    module via importlib.util but never put it in sys.modules), dataclass
    instantiation fails with ``AttributeError: 'NoneType' object has no
    attribute '__dict__'`` (bug 5be7 chain — defect #4 / chain item 4).

    Registration must happen BEFORE ``exec_module`` so that any decorator
    that runs during module body execution (e.g. ``@dataclass``) sees the
    module already in sys.modules.
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


def _run_reconcile_check(repo_root: Path) -> int:
    """Execute a read-only reconciliation check and report discrepancies.

    Returns 0 on success, 1 on error.
    """
    rc_mod = _try_load_step("reconcile_check")
    if rc_mod is None:
        print("ERROR: reconcile_check.py not found", file=sys.stderr)
        return 1

    fetcher = _try_load_step("fetcher")
    if fetcher is None:
        print("ERROR: fetcher.py not found — cannot load Jira snapshot", file=sys.stderr)
        return 1

    try:
        # Fetch current Jira snapshot. reconcile-check is read-only — use
        # compute_snapshot (no bridge_state/snapshots/<pass>.json write) so the
        # diagnostic does not mutate the local store (ticket yaw-plait-doe).
        pass_id = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")

        jira_snapshot = fetcher.compute_snapshot(pass_id, repo_root)

        # Load local tickets from .tickets-tracker. Bug ad39: the event-sourced
        # store has no per-ticket ticket.json — the compiled ticket lives in
        # <id>/.cache.json["state"]. rc_mod.load_local_tickets reads that (the
        # old ticket.json read loaded nothing → all bindings reported orphaned).
        tracker_dir = repo_root / ".tickets-tracker"  # tickets-boundary-ok
        local_tickets: list[dict] = rc_mod.load_local_tickets(tracker_dir)

        # Load binding store. BindingStore lives in binding_store.py — not in
        # applier.py (the previous lookup `hasattr(applier, "BindingStore")`
        # always failed because applier.py never exported the class, falling
        # through to a list-returning stub that crashed reconcile_check's
        # `.items()` call). Bug 0776: load binding_store.py directly via the
        # same factory reconcile.py uses.
        binding_store_mod = _try_load_step("binding_store")
        if binding_store_mod is None or not hasattr(binding_store_mod, "load_binding_store"):
            # Minimal stub: no bindings. all_bindings() returns a dict to
            # match the protocol reconcile_check expects.
            class _EmptyBindings:
                def all_bindings(self) -> dict:
                    return {}

            binding_store = _EmptyBindings()
        else:
            binding_store = binding_store_mod.load_binding_store(repo_root)

        report = rc_mod.reconcile_check(local_tickets, jira_snapshot, binding_store)
        print(rc_mod.format_report(report))

        # Write JSON report
        output_path = repo_root / "bridge_state" / "reconcile-check.json"
        rc_mod.write_report_json(report, output_path)
        print(f"\nFull report written to {output_path}")
        return 0
    except Exception as exc:  # noqa: BLE001 — CLI top-level: log and return exit code 1
        print(f"ERROR: reconcile-check failed: {exc}", file=sys.stderr)
        return 1


_LEGACY_LOCK_FILES = (".reconciler-pass-lock", ".reconciler-phase-gate")


def _lock_steal_enabled() -> bool:
    """Whether the held-lock path may steal an expired lease (story 9622).

    Kill-switch ``REBAR_RECONCILER_LOCK_STEAL`` — default ON. Only an explicit
    falsy value (``0``/``false``/``no``/``off``/empty) reverts to the old
    unconditional exit-3 behavior (ops back-out without a deploy).
    """
    return os.environ.get("REBAR_RECONCILER_LOCK_STEAL", "1").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
        "",
    )


def _resolve_held_lock(advisory, pass_id, repo_root, *, acquire_fn):
    """Resolve a HELD pass lock via steal (story 9622). Steal-enabled precondition.

    Returns ``(exit_code, lock_oid, acquired)``:
      - steal wins (a new oid)              -> ``(None, stolen_oid, True)``  [case 1: adopt]
      - steal None + ref still held          -> ``(3, None, False)``          [case 2: live holder]
      - steal None + freed + acquire wins    -> ``(None, acquired_oid, True)``[case 3a]
      - steal None + freed + acquire loses   -> ``(3, None, False)``          [case 3b]

    ``steal()`` (via ``advisory.steal_pass_lock``) IS the skew-proof expiry test —
    a returned oid means the lease was stale. ``None`` means the holder is live OR
    the ref freed during the steal sleep; a re-read discriminates. On the freed
    fork we acquire normally via ``acquire_fn`` (a lost race raises
    ``advisory.ReconcileLockError`` -> yield).
    """
    stolen_oid = advisory.steal_pass_lock(pass_id, repo_root)
    if stolen_oid is not None:
        return (None, stolen_oid, True)
    if advisory.check_pass_lock(repo_root):
        return (_Disposition.IN_FLIGHT, None, False)
    # freed during our steal sleep -> acquire normally (win: proceed; lose: yield).
    try:
        return (None, acquire_fn(), True)
    except advisory.ReconcileLockError:
        return (_Disposition.IN_FLIGHT, None, False)


def _purge_committed_reconciler_locks(repo_root: Path) -> None:
    """Remove any legacy ``.reconciler-*`` lock files still committed on the tickets
    branch (epic dust-troth-naval / C4 migration).

    The lock moved to ``refs/reconciler/*``; a repo initialized under the old file
    backend may still carry committed ``.reconciler-pass-lock`` / ``.reconciler-phase-gate``
    blobs on the ``tickets`` branch. This deletes them once via a single ref-advance
    CAS commit. Idempotent (no-op when none are present) and best-effort: any git
    failure is logged and swallowed so it never aborts the pass.
    """
    from rebar_reconciler import git_adapter

    try:
        present = [
            f
            for f in _LEGACY_LOCK_FILES
            if git_adapter.cat_file_exists(repo_root, f"{git_adapter.TICKETS_BRANCH}:{f}")
        ]
        if not present:
            return
        old = git_adapter.rev_parse(
            repo_root, git_adapter.TICKETS_BRANCH, check=True
        ).stdout.strip()
        # Prune the legacy paths in a DETACHED temp index (read-tree → rm --cached →
        # write-tree → commit-tree), then CAS-advance refs/heads/tickets — the main
        # worktree/index is never touched, and the CAS makes a concurrent writer safe.
        env = {**os.environ, "GIT_INDEX_FILE": str(repo_root / ".git" / "reconciler-purge-index")}
        git_adapter.read_tree(repo_root, old, env=env)
        git_adapter.rm_cached(repo_root, *present, env=env)
        new_tree = git_adapter.write_tree(repo_root, env=env)
        new_commit = git_adapter.commit_tree(
            repo_root,
            new_tree,
            parent=old,
            message=(
                "chore(reconciler): drop legacy .reconciler-* lock files "
                "(moved to refs/reconciler/*)"
            ),
            env=env,
        )
        git_adapter.update_ref(repo_root, git_adapter.TICKETS_REF, new_commit, old)
        print(
            f"reconcile: purged legacy committed lock files {present} from the tickets branch",
            file=sys.stderr,
        )
    except Exception as exc:  # noqa: BLE001 — migration is best-effort, never aborts the pass
        print(f"WARN: legacy .reconciler-* purge skipped: {exc!r}", file=sys.stderr)


class _Heartbeat:
    """Daemon-thread lease heartbeat for the ref-lock backend (epic dust-troth-naval).

    Renews the pass lease every ``interval`` seconds via
    ``advisory.renew_pass_lock``. On a lost/stolen lease it sets ``lock_lost`` and
    stops (a daemon thread cannot raise into the main thread — the main pass polls
    ``lock_lost`` at per-mutation checkpoints and aborts). Other (transient) renew
    errors are logged and retried on the next tick. The latest oid is published
    back so the ``finally`` release CASes against the right value.
    """

    def __init__(self, advisory_mod, pass_id: str, repo_root: Path, oid: str, interval: int):
        self._advisory = advisory_mod
        self._pass_id = pass_id
        self._repo_root = repo_root
        self._oid = oid
        self._interval = interval
        self.lock_lost = threading.Event()
        self._stop = threading.Event()
        self._oid_lock = threading.Lock()
        self._thread: threading.Thread | None = None
        self._lease_lost_cls = advisory_mod._load_ref_lock().LeaseLostError

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="reconciler-heartbeat", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            try:
                new_oid = self._advisory.renew_pass_lock(
                    self._pass_id, self._repo_root, self.current_oid()
                )
                with self._oid_lock:
                    self._oid = new_oid
            except self._lease_lost_cls:
                # Bug 4afc-33cc-9e4f-4fe2: probe what the ref actually holds NOW.
                # Reporting only that the lease is gone leaves "stolen by whom"
                # unanswerable — the ambiguity that made that bug's lease losses
                # unclassifiable after the fact. A DIFFERENT holder is a real takeover;
                # our own oid still on the ref means the CAS failed for another reason.
                # Strictly diagnostic: it must never mask or delay the abort, and a
                # probe that fails says so rather than reporting nothing (silence would
                # read as "no holder", which is the ambiguity being removed).
                try:
                    _rl = self._advisory._load_ref_lock()
                    _st = _rl.read(
                        self._repo_root,
                        _rl.LOCK_REF,
                        remote=self._advisory._lock_remote(self._repo_root),
                    )
                    held = (
                        f"ref now oid={_st.oid} holder={_st.holder!r} fence={_st.fence}"
                        if _st is not None
                        else "ref now ABSENT"
                    )
                except Exception as exc:  # noqa: BLE001 — diagnostic must not mask the abort
                    held = f"ref state UNREADABLE ({exc!r})"
                print(
                    f"ERROR: reconcile heartbeat lost the lease "
                    f"(pass_id={self._pass_id!r}, we held {self.current_oid()}; {held})"
                    f" — aborting pass",
                    file=sys.stderr,
                )
                self.lock_lost.set()
                return
            except Exception as exc:  # noqa: BLE001 — transient renew error: log + retry
                print(
                    f"WARN: reconcile heartbeat renew failed (retrying): {exc!r}", file=sys.stderr
                )

    def current_oid(self) -> str:
        with self._oid_lock:
            return self._oid

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=self._interval + 5)


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


def _reconcile_exception_exit(
    exc: Exception,
    *,
    route: str | None,
    reschedule_error_cls,
    lock_lost_cls,
) -> int:
    """Classify one failed reconcile_once call, then translate it by route."""
    if type(exc).__name__ == "SelectionStaleError":
        return _finish_disposition(
            _Disposition.INVALID_INVOCATION, route, legacy_message=f"ERROR: {exc}"
        )
    if reschedule_error_cls is not None and isinstance(exc, reschedule_error_cls):
        message = f"RESCHEDULE: reconcile_once signalled reschedule: {exc}"
        return _finish_disposition(_Disposition.RESCHEDULE, route, legacy_message=message)
    if lock_lost_cls is not None and isinstance(exc, lock_lost_cls):
        message = f"RESCHEDULE: pass lock lease lost mid-pass: {exc}"
        return _finish_disposition(_Disposition.RESCHEDULE, route, legacy_message=message)
    return _finish_disposition(
        _Disposition.OPERATIONAL_FAILURE,
        route,
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
    if repo_root is None:
        repo_root = Path(os.environ.get("REBAR_ROOT") or Path(__file__).resolve().parents[4])

    reconcile = _try_load_step("reconcile")
    if reconcile is None:
        if route not in {"preview", "sync"}:
            print("OK: no-op (reconcile.py not present in this deployment)")
        return _finish_disposition(_Disposition.CONVERGED, route)

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
        return _reconcile_exception_exit(
            exc,
            route=route,
            reschedule_error_cls=reschedule_error_cls,
            lock_lost_cls=lock_lost_cls,
        )

    computed = result.get("mutation_count", 0)
    applied = result.get("mutations_applied", computed)
    failures = result.get("mutation_failures", 0)

    # No-write (cap-0) modes (dry-run / reconcile-check via reconcile_once):
    # emit the COMPUTED plan as JSON to STDOUT so library callers
    # (rebar.reconcile) and MCP receive the full plan. The human-readable
    # OK/RECON summary goes to STDERR so it does not corrupt the JSON payload.
    # Writing-mode output shape is unchanged (OK line on stdout, no JSON).
    if result.get("no_write"):
        import json as _json

        if route not in {"preview", "sync"}:
            print(
                f"OK: dry-run computed {computed} mutations (0 applied, no writes)",
                file=sys.stderr,
            )
        print(_json.dumps(result))
        return _finish_disposition(_Disposition.CONVERGED, route)

    if route not in {"preview", "sync"}:
        if computed == 0 and applied == 0:
            print("OK: steady-state pass converged — 0 mutations")
        elif failures == 0:
            print(f"OK: applied {applied} of {computed} mutations")
        else:
            print(f"OK: applied {applied} of {computed} mutations ({failures} failed)")
    # Per-mutation failures are operational failures; successful fallbacks are
    # counted as applied by reconcile.py and therefore remain converged.
    if failures > 0:
        return _finish_disposition(
            _Disposition.OPERATIONAL_FAILURE,
            route,
            canonical_message=f"ERROR: reconcile completed with {failures} mutation failures",
        )
    return _finish_disposition(_Disposition.CONVERGED, route)


def _resolve_request_selection(request) -> tuple[set[str] | None, str | None]:
    """Resolve canonical selection tokens before any pass-lock inspection."""
    if not request.selection_tokens:
        return None, None
    helpers = _load_sibling_keyed(_HELPERS_KEY, "reconcile_helpers.py")
    try:
        return helpers.resolve_selection(request.repo_root, request.selection_tokens), None
    except helpers.SelectionError as exc:
        return None, str(exc)


def _dry_run_enumeration_exit(request) -> int | None:
    """Handle the compatibility directory-enumeration route before lock checks."""
    if not request.dry_run_enumerate:
        return None
    repo_root = request.repo_root
    resolved_root = (
        repo_root
        if repo_root is not None
        else Path(os.environ.get("REBAR_ROOT") or Path(__file__).resolve().parents[4])
    )
    tickets_dir = resolved_root / ".tickets-tracker"
    if not tickets_dir.is_dir():
        return 0
    for entry in sorted(tickets_dir.iterdir()):
        if not entry.is_dir():
            continue
        if ".scratch" in entry.parts:
            continue
        print(entry)
    return 0


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

    # This compatibility path remains before mode and advisory-lock checks.
    enumeration_exit = _dry_run_enumeration_exit(request)
    if enumeration_exit is not None:
        return enumeration_exit

    # -------------------------------------------------------------------------
    # Step 1: Mode validation (dd-2) — BEFORE any fetcher reference.
    # Load mode.py under the dotted key so tests can pre-seed sys.modules.
    # -------------------------------------------------------------------------
    target_mode = request.target_mode

    # -------------------------------------------------------------------------
    # Step 1b: reconcile-check mode — read-only diagnostic, no lock needed.
    # -------------------------------------------------------------------------
    if target_mode == mode_mod.Mode.RECONCILE_CHECK:
        return _run_reconcile_check(repo_root)

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

    # -------------------------------------------------------------------------
    # Step 2: Advisory lock + phase-gate checks.
    # Load _advisory_lock under the dotted key so tests can pre-seed sys.modules.
    # -------------------------------------------------------------------------
    advisory = _load_sibling_keyed(_ADVISORY_LOCK_KEY, "_advisory_lock.py")

    pause_exit = _pause_exit_code(advisory, target_mode, mode_mod, repo_root, route)
    if pause_exit is not None:
        return pause_exit

    # One-time migration (epic dust-troth-naval / C4): the lock moved to
    # refs/reconciler/*; scrub any pre-existing .reconciler-* lock files still
    # committed on the tickets branch from the old file backend. Idempotent; a git
    # failure logs and continues (never aborts the pass).
    if selection_ids is None:
        _purge_committed_reconciler_locks(repo_root)

    # Generate pass_id ONCE, up-front — it is both the lock/steal HOLDER and is
    # threaded into run_pass(). (Previously generated at Step 3, below the lock
    # check; hoisted here for story 9622 so the steal attempt has a holder.) Under
    # any sub-second clock advance a second timestamp could diverge from the lock
    # owner — a silent hazard for post-mortems correlating locks to pass records.
    pass_id = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")

    # Step 2a: pass-lock check (dd-3). If held, attempt to STEAL an expired lease
    # (story 9622) instead of unconditionally exiting 3 — a SIGKILLed pass would
    # otherwise wedge refs/reconciler/lock until an operator hand-deleted it. Gated
    # by REBAR_RECONCILER_LOCK_STEAL (default ON; OFF = old unconditional exit-3).
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
        held_disposition, pre_acquired_oid, _ok = _resolve_held_lock(
            advisory,
            pass_id,
            repo_root,
            acquire_fn=lambda: advisory.acquire_pass_lock(pass_id, repo_root),
        )
        if held_disposition is not None:
            return _finish_disposition(
                held_disposition,
                route,
                legacy_message=("reconcile: refs/reconciler/lock held by a live pass — yielding"),
            )

    # -------------------------------------------------------------------------
    # Step 3: acquire (or adopt the stolen lock), run pass, release in finally.
    # -------------------------------------------------------------------------
    # Bug b859: acquire_pass_lock was previously OUTSIDE the try/except so
    # ReconcileLockError (or any pre-run_pass exception) escaped uncaught as
    # a raw Python traceback — invisible to operators / probes that look
    # for the ``ERROR:`` prefix. Move acquire_pass_lock INTO the try, gated
    # by an ``acquired`` flag so the finally clause only releases when we
    # actually held the lock. Diagnostic tracebacks are emitted to stderr
    # so the probe's unfiltered side-car log captures them too.
    acquired = False
    lock_oid: str | None = None
    heartbeat: _Heartbeat | None = None
    abort_check = None
    try:
        # Adopt the stolen/freed-acquired oid (story 9622) — skip acquire_pass_lock,
        # which would otherwise re-CAS the ref we already own — or acquire normally
        # (the not-held path).
        if pre_acquired_oid is not None:
            lock_oid = pre_acquired_oid
        else:
            lock_oid = advisory.acquire_pass_lock(pass_id, repo_root)
        acquired = True
        # The ref-lock backend returns an oid; start the daemon heartbeat that renews
        # the lease at max(1, lease//3) and build the per-mutation abort checkpoint that
        # raises ReconcileLockLost if the heartbeat loses the lease mid-pass. A test
        # stub of `advisory` returning None (no ref backend) simply skips the heartbeat.
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

        return run_pass(
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
    except Exception as exc:  # noqa: BLE001 — CLI top-level: log + traceback, return exit code 1
        # Print the prefixed line first so grep-based probes see it, THEN
        # the traceback so operators can root-cause. Both go to stderr.
        print(f"ERROR: run_pass raised: {exc}", file=sys.stderr)
        import traceback as _tb

        _tb.print_exc(file=sys.stderr)
        return 1
    finally:
        # Stop the heartbeat first so it no longer advances the ref, then release
        # against the LATEST oid it renewed to (a stale/absent ref no-ops).
        if heartbeat is not None:
            heartbeat.stop()
        if acquired:
            try:
                if heartbeat is not None:
                    # Ref backend: release against the LATEST renewed oid.
                    advisory.release_pass_lock(pass_id, repo_root, oid=heartbeat.current_oid())
                else:
                    # File backend (or a test stub with a 2-arg release).
                    advisory.release_pass_lock(pass_id, repo_root)
            except Exception as _rel_exc:  # noqa: BLE001 — release in finally must not mask original error
                # Release failure must not mask the original error path.
                print(
                    f"WARN: release_pass_lock failed for pass_id={pass_id!r}: {_rel_exc!r}",
                    file=sys.stderr,
                )


if __name__ == "__main__":
    sys.exit(main())
