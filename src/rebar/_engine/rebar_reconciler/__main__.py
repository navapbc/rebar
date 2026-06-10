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

import argparse
import datetime
import importlib
import importlib.util
import os
import sys
from pathlib import Path

# Dotted-name keys used for sys.modules seeding so that both production code
# and unit tests (which pre-seed sys.modules with these exact keys) share the
# same module objects and patch() targets resolve correctly.
_ADVISORY_LOCK_KEY = "rebar_reconciler._advisory_lock"
_MODE_KEY = "rebar_reconciler.mode"


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
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
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
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


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
        print(
            "ERROR: fetcher.py not found — cannot load Jira snapshot", file=sys.stderr
        )
        return 1

    try:
        # Fetch current Jira snapshot. reconcile-check is read-only — use
        # compute_snapshot (no bridge_state/snapshots/<pass>.json write) so the
        # diagnostic does not mutate the local store (ticket yaw-plait-doe).
        pass_id = datetime.datetime.now(datetime.timezone.utc).strftime(
            "%Y-%m-%dT%H-%M-%S"
        )
        import json as _json

        jira_snapshot = fetcher.compute_snapshot(pass_id, repo_root)

        # Load local tickets from .tickets-tracker
        tracker_dir = repo_root / ".tickets-tracker"  # tickets-boundary-ok
        local_tickets: list[dict] = []
        if tracker_dir.is_dir():
            for entry in sorted(tracker_dir.iterdir()):
                if not entry.is_dir() or ".scratch" in entry.parts:
                    continue
                meta_path = entry / "ticket.json"
                if meta_path.exists():
                    ticket = _json.loads(meta_path.read_text())
                    if "id" not in ticket:
                        ticket["id"] = entry.name
                    local_tickets.append(ticket)

        # Load binding store. BindingStore lives in binding_store.py — not in
        # applier.py (the previous lookup `hasattr(applier, "BindingStore")`
        # always failed because applier.py never exported the class, falling
        # through to a list-returning stub that crashed reconcile_check's
        # `.items()` call). Bug 0776: load binding_store.py directly via the
        # same factory reconcile.py uses.
        binding_store_mod = _try_load_step("binding_store")
        if binding_store_mod is None or not hasattr(
            binding_store_mod, "load_binding_store"
        ):
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
    except Exception as exc:  # noqa: BLE001
        print(f"ERROR: reconcile-check failed: {exc}", file=sys.stderr)
        return 1


def run_pass(
    repo_root: Path | None = None,
    pass_id: str | None = None,
    target_mode=None,
    filter_local_ids: set[str] | None = None,
) -> int:
    """Execute one steady-state reconciliation pass via reconcile.reconcile_once().

    Returns 0 on converged state, EXIT_RESCHEDULE (75) when applier signals a
    reschedule (rebase_retry exhausted), 1 on any other unrecoverable error.

    When *pass_id* is None (legacy entry-point), one is generated here so the
    helper remains usable in isolation. Production callers should pass the
    pass_id from main() so the lock-holder and the recorded reconcile pass
    share the same identifier — previously two distinct timestamps were
    generated and a sub-second race could record mismatched pass_ids.
    """
    if repo_root is None:
        repo_root = Path(os.environ.get("REBAR_ROOT") or os.environ.get("PROJECT_ROOT") or Path(__file__).resolve().parents[4])

    reconcile = _try_load_step("reconcile")
    if reconcile is None:
        # Graceful no-op when reconcile.py is absent in the current deployment
        # (e.g., orchestrator deployed ahead of the reconcile module).
        print("OK: no-op (reconcile.py not present in this deployment)")
        return 0

    # F6: load the applier module so RescheduleError + EXIT_RESCHEDULE are
    # available for explicit handling. Without this, the broad `except
    # Exception` below would mask RescheduleError under exit 1, hiding the
    # reschedule signal from any scheduler that distinguishes 75 from 1.
    applier = _try_load_step("applier")

    if pass_id is None:
        pass_id = datetime.datetime.now(datetime.timezone.utc).strftime(
            "%Y-%m-%dT%H-%M-%S"
        )
    reschedule_error_cls = (
        getattr(applier, "RescheduleError", None) if applier else None
    )
    exit_reschedule = getattr(applier, "EXIT_RESCHEDULE", 75) if applier else 75

    try:
        result = reconcile.reconcile_once(
            pass_id,
            repo_root=repo_root,
            target_mode=target_mode,
            filter_local_ids=filter_local_ids,
        )
    except Exception as exc:  # noqa: BLE001
        if reschedule_error_cls is not None and isinstance(exc, reschedule_error_cls):
            print(
                f"RESCHEDULE: reconcile_once signalled reschedule: {exc}",
                file=sys.stderr,
            )
            return exit_reschedule
        print(f"ERROR: reconcile_once raised: {exc}", file=sys.stderr)
        return 1

    # Bug 85a1: truthful tally. Before this fix, the message printed
    # mutation_count (computed pre-apply) under the verb "converged", which
    # was structurally lying when mutations errored out mid-pass. Now:
    #   - applied > 0       → "OK: applied N (F failed) — pass <id>"
    #   - applied == 0 and computed == 0 → "OK: steady-state pass converged"
    #   - applied == 0 and computed > 0  → "OK: applied 0 of N (N failed) — pass <id>"
    # The "converged" verb is reserved for genuine no-op passes (computed=0).
    # Legacy callers that read mutation_count from reconcile_once's return
    # still work; this only changes the human-readable stdout line.
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

        print(
            f"OK: dry-run computed {computed} mutations (0 applied, no writes)",
            file=sys.stderr,
        )
        print(_json.dumps(result))
        return 0

    if computed == 0 and applied == 0:
        print("OK: steady-state pass converged — 0 mutations")
    elif failures == 0:
        print(f"OK: applied {applied} of {computed} mutations")
    else:
        print(
            f"OK: applied {applied} of {computed} mutations "
            f"({failures} failed)"
        )
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
    parser = argparse.ArgumentParser(prog="rebar_reconciler")
    parser.add_argument(
        "--repo-root",
        default=None,
        help="Repository root (default: auto-detect from script location)",
    )
    # --mode is NOT required; omitting it defaults to 'live' so that
    # inject-and-heal.sh (which calls 'python3 -m rebar_reconciler --repo-root ...'
    # with no --mode flag) continues to work with the steady-state production mode.
    parser.add_argument(
        "--mode",
        default=None,
        help=(
            "Rollout-safety mode: reconcile-check | dry-run | bootstrap-strict "
            "| bootstrap-throttle | live (default: live)"
        ),
    )
    parser.add_argument(
        "--dry-run-enumerate",
        action="store_true",
        default=False,
        help=(
            "Print the list of ticket-tracker entries that the reconciler would enumerate "
            "(after .scratch/ exclusion) and exit without running a pass. "
            "Each entry is printed as an absolute path, one per line."
        ),
    )
    parser.add_argument(
        "--filter-local-ids",
        default=None,
        help=(
            "Comma-separated list of local ticket IDs.  When set, all three "
            "differs run on their full unfiltered inputs (same code paths as "
            "production) but only mutations targeting these IDs (or their "
            "bound Jira keys) reach the applier.  For validation use only."
        ),
    )
    args = parser.parse_args(argv)
    # Default to the project repo root when --repo-root is omitted. Mirrors
    # run_pass()'s default at lines 84-85 so the four advisory_lock guard
    # calls below (which declare repo_root: Path, not Optional) never see
    # None and accidentally invoke `git -C None ...` (bug 5be7-d657-1dde-4237).
    repo_root = (
        Path(args.repo_root) if args.repo_root else Path(os.environ.get("REBAR_ROOT") or os.environ.get("PROJECT_ROOT") or Path(__file__).resolve().parents[4])
    )

    # --dry-run-enumerate: list enumerable ticket directories and exit.
    # This path is intentionally placed before advisory-lock and mode checks so
    # the flag is usable in test fixtures without a live Jira config or lock state.
    if getattr(args, "dry_run_enumerate", False):
        resolved_root = (
            repo_root if repo_root is not None else Path(os.environ.get("REBAR_ROOT") or os.environ.get("PROJECT_ROOT") or Path(__file__).resolve().parents[4])
        )
        tickets_dir = resolved_root / ".tickets-tracker"
        if not tickets_dir.is_dir():
            # No tracker directory — emit nothing and exit cleanly.
            return 0
        for entry in sorted(tickets_dir.iterdir()):
            if not entry.is_dir():
                continue
            # Apply the same .scratch/ exclusion used by health.py walkers.
            if ".scratch" in entry.parts:
                continue
            print(entry)
        return 0

    # -------------------------------------------------------------------------
    # Step 1: Mode validation (dd-2) — BEFORE any fetcher reference.
    # Load mode.py under the dotted key so tests can pre-seed sys.modules.
    # -------------------------------------------------------------------------
    mode_mod = _load_sibling_keyed(_MODE_KEY, "mode.py")
    mode_str = args.mode if args.mode is not None else mode_mod.Mode.LIVE.value
    try:
        target_mode = mode_mod.Mode.from_str(mode_str)
    except ValueError as exc:
        print(str(exc), file=sys.stderr)
        return 2

    # -------------------------------------------------------------------------
    # Step 1b: reconcile-check mode — read-only diagnostic, no lock needed.
    # -------------------------------------------------------------------------
    if target_mode == mode_mod.Mode.RECONCILE_CHECK:
        return _run_reconcile_check(repo_root)

    # -------------------------------------------------------------------------
    # Step 2: Advisory lock + phase-gate checks.
    # Load _advisory_lock under the dotted key so tests can pre-seed sys.modules.
    # -------------------------------------------------------------------------
    advisory = _load_sibling_keyed(_ADVISORY_LOCK_KEY, "_advisory_lock.py")

    # Step 2a: pass-lock check (dd-3)
    if advisory.check_pass_lock(repo_root):
        print(
            "reconcile: .reconciler-pass-lock present on tickets branch "
            "— another pass in flight",
            file=sys.stderr,
        )
        return 3

    # Step 2b: phase-gate check (dd-4)
    if advisory.check_phase_gate(target_mode, repo_root):
        print(
            f"reconcile: .reconciler-phase-gate blocks advancement to "
            f"{target_mode.value}; remove the file from tickets to advance",
            file=sys.stderr,
        )
        return 4

    # -------------------------------------------------------------------------
    # Step 3: acquire lock, run pass, release in finally
    #
    # Generate pass_id ONCE here and thread it into both the lock-holder and
    # run_pass(). Previously run_pass generated a second timestamp, so under
    # any sub-second clock advance the recorded reconcile pass_id could
    # diverge from the lock owner pass_id — silent operational hazard for
    # post-mortems correlating locks to pass records.
    # -------------------------------------------------------------------------
    pass_id = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%dT%H-%M-%S")
    # Bug b859: acquire_pass_lock was previously OUTSIDE the try/except so
    # ReconcileLockError (or any pre-run_pass exception) escaped uncaught as
    # a raw Python traceback — invisible to operators / probes that look
    # for the ``ERROR:`` prefix. Move acquire_pass_lock INTO the try, gated
    # by an ``acquired`` flag so the finally clause only releases when we
    # actually held the lock. Diagnostic tracebacks are emitted to stderr
    # so the probe's unfiltered side-car log captures them too.
    acquired = False
    try:
        advisory.acquire_pass_lock(pass_id, repo_root)
        acquired = True
        filter_local_ids: set[str] | None = None
        if args.filter_local_ids is not None:
            parsed = {
                s.strip() for s in args.filter_local_ids.split(",") if s.strip()
            }
            if not parsed:
                print(
                    "ERROR: --filter-local-ids must contain at least one "
                    "non-empty ID",
                    file=sys.stderr,
                )
                return 2
            filter_local_ids = parsed
        return run_pass(
            repo_root=repo_root,
            pass_id=pass_id,
            target_mode=target_mode,
            filter_local_ids=filter_local_ids,
        )
    except Exception as exc:  # noqa: BLE001
        # Print the prefixed line first so grep-based probes see it, THEN
        # the traceback so operators can root-cause. Both go to stderr.
        print(f"ERROR: run_pass raised: {exc}", file=sys.stderr)
        import traceback as _tb
        _tb.print_exc(file=sys.stderr)
        return 1
    finally:
        if acquired:
            try:
                advisory.release_pass_lock(pass_id, repo_root)
            except Exception as _rel_exc:  # noqa: BLE001
                # Release failure must not mask the original error path.
                print(
                    f"WARN: release_pass_lock failed for pass_id={pass_id!r}: "
                    f"{_rel_exc!r}",
                    file=sys.stderr,
                )


if __name__ == "__main__":
    sys.exit(main())
