#!/usr/bin/env python3
"""reconcile.py — one-pass orchestrator: fetch → diff → apply.

reconcile_once(pass_id) wires the three reconciler stages into a single
idempotent pass.  Two consecutive calls with an unchanged remote produce
mutation_count=0 on both passes (second call sees prev==curr snapshot).
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sys
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ``lazy_load`` centralizes the by-path sibling-loader idiom (rebar_reconciler/
# _loader.py). Import it normally when package context exists, else bootstrap it
# by file path — this module is itself exec'd standalone via
# spec_from_file_location in tests.
try:
    from rebar_reconciler._loader import lazy_load
except ImportError:  # standalone load without package context
    _loader_key = "rebar_reconciler._loader"
    if _loader_key not in sys.modules:
        _loader_spec = importlib.util.spec_from_file_location(
            _loader_key, Path(__file__).parent / "_loader.py"
        )
        assert _loader_spec is not None and _loader_spec.loader is not None
        _loader_mod = importlib.util.module_from_spec(_loader_spec)
        sys.modules[_loader_key] = _loader_mod
        _loader_spec.loader.exec_module(_loader_mod)  # type: ignore[union-attr]
    lazy_load = sys.modules[_loader_key].lazy_load


def _load(name: str, relpath: str):
    """Load a sibling module by relative file path, registering it in sys.modules.

    Returns the cached module when ``name`` is already in ``sys.modules``;
    this allows test fixtures to pre-register patched modules and have
    ``reconcile_once`` reuse them rather than loading fresh copies. Delegates to
    the shared ``lazy_load`` helper (the package-wide by-path loader).
    """
    return lazy_load(name, relpath)


# ---------------------------------------------------------------------------
# Leaf-helper re-exports. reconcile_helpers.py holds the pure pass-support
# utilities that carry no back-edge to the reconcile_once spine (status
# preflight, binding-store commit-back, the inbound-probe router, the ticket-CLI
# reader, the filter-scope builders, the no-write plan renderer, and the cap-0
# sync-logger stand-in). Load it once by path and bind its names at module level
# so (a) the staying phase helpers call them as bare names — preserving the
# monkeypatch seam tests rely on — and (b) attribute access (``reconcile.<name>``,
# used by tests that load this module by path) keeps resolving all ten names.
# ---------------------------------------------------------------------------
_helpers = _load("reconcile_helpers", "reconcile_helpers.py")

StatusMappingError = _helpers.StatusMappingError
preflight_status_mapping = _helpers.preflight_status_mapping
_commit_binding_store_snapshot = _helpers._commit_binding_store_snapshot
_audit_log_probe = _helpers._audit_log_probe
route_inbound_probe = _helpers.route_inbound_probe
_read_local_tickets = _helpers._read_local_tickets
_build_filter_target_set = _helpers._build_filter_target_set
_mutation_matches_filter = _helpers._mutation_matches_filter
_build_plan_entries = _helpers._build_plan_entries
_NoOpSyncLogger = _helpers._NoOpSyncLogger


@dataclass
class _PassContext:
    """Mutable per-pass state threaded through reconcile_once's phase helpers.

    reconcile_once is a thin sequencer over _load_snapshots -> run_differs ->
    _apply_mutations -> _persist_and_log; each phase reads the fields it needs
    and writes back the ones it produces. Carrying the ~30 threaded values on one
    object (rather than as positional params) keeps each phase independently
    callable + unit-testable while preserving the single-pass idempotent contract.
    """

    # inputs (set at construction)
    pass_id: str
    repo_root: Path
    target_mode: Any = None
    filter_local_ids: set[str] | None = None
    # optional per-mutation lost-lease checkpoint (epic dust-troth-naval): a
    # zero-arg callable the applier invokes before each mutation; it raises
    # (ReconcileLockLost) if the ref-lock heartbeat lost the lease. None = no-op.
    abort_check: Any = None
    # populated by _load_snapshots
    persist: bool = True
    fetcher: Any = None
    differ: Any = None
    applier: Any = None
    health_mod: Any = None
    invariants_mod: Any = None
    binding_store_mod: Any = None
    outbound_differ_mod: Any = None
    inbound_differ_mod: Any = None
    local_label_intent_mod: Any = None
    sync_logger_mod: Any = None
    mode_mod: Any = None
    sync_logger: Any = None
    local_tickets: list = field(default_factory=list)
    binding_store: Any = None
    tracker_dir: Path | None = None
    prev_path: Path | None = None
    prev_snapshot: dict = field(default_factory=dict)
    curr_path: Path | None = None
    curr_snapshot: dict = field(default_factory=dict)
    # populated by run_differs (sibling run_differs.py)
    mutations: list = field(default_factory=list)
    # pending-binding recovery failures this pass (story 9622); tally-only, not a gate
    recovery_failures: int = 0
    # populated by _apply_mutations
    unfiltered_count: int = 0
    manifest_path: Any = None
    nowrite_plan: dict | None = None


def reconcile_once(
    pass_id: str,
    repo_root: Path | None = None,
    target_mode=None,
    filter_local_ids: set[str] | None = None,
    abort_check=None,
) -> dict:
    """Run one reconciler pass: fetch → diff → apply.

    Reads the previous snapshot (written at the end of the prior pass) from
    ``bridge_state/snapshots/<pass_id>.prev.json``, fetches the current
    remote state, computes mutations, applies them, then advances the prev
    snapshot file so the next call is idempotent against an unchanged remote.

    The pass now includes bidirectional sync:
      1. Legacy inbound path (snapshot diff → typed Mutations)
      2. Outbound path (local→Jira via outbound_differ + binding_store)
      3. New inbound path (Jira→local via inbound_differ for bound tickets)
      4. Sync logger for structured audit trail
      5. Binding store persistence at pass end

    Args:
        pass_id:       Unique identifier for this reconciliation pass.
        repo_root:     Repository root directory.  Defaults to four levels
                       above this file (rebar_reconciler/ → _engine/ → rebar/ →
                       src/ → repo root).
        filter_local_ids:
                       When set, restricts which mutations reach the applier.
                       All three differs run on their full, unfiltered inputs
                       (same code paths as production).  Only mutations whose
                       target or provenance matches a local ID in this set
                       (or its bound Jira key) are dispatched.  ``None``
                       (default) means no filtering — full reconciliation.

    Returns:
        ``{"pass_id": pass_id, "mutation_count": N, "manifest_path": str}``
        where N is the number of mutations dispatched in this pass.
    """
    if repo_root is None:
        repo_root = Path(os.environ.get("REBAR_ROOT") or Path(__file__).resolve().parents[4])
    ctx = _PassContext(
        pass_id=pass_id,
        repo_root=repo_root,
        target_mode=target_mode,
        filter_local_ids=filter_local_ids,
        abort_check=abort_check,
    )
    _load_snapshots(ctx)
    # Diff phase lives in the sibling run_differs.py (loaded lazily by file path,
    # matching the sibling-loader convention). route_inbound_probe is passed in
    # (rather than imported) so run_differs.py holds no back-edge to reconcile.py —
    # route_inbound_probe now lives in the sibling reconcile_helpers.py and is re-exported
    # here (a separately-tested public surface).
    run_differs_mod = _load("reconcile_run_differs", "run_differs.py")
    run_differs_mod.run_differs(ctx, route_inbound_probe)
    _apply_mutations(ctx)
    return _persist_and_log(ctx)


def _load_snapshots(ctx: _PassContext) -> None:
    """Load phase: sibling modules + persist flag + sync logger + local tickets +
    binding store + the prev/curr snapshots (aborting via _handle_corrupt_snapshot
    on a corrupt prev_snapshot). Populates ctx for the diff/apply/persist phases.
    """
    pass_id = ctx.pass_id
    repo_root = ctx.repo_root
    target_mode = ctx.target_mode
    filter_local_ids = ctx.filter_local_ids
    fetcher = _load("reconcile_fetcher", "fetcher.py")
    differ = _load("reconcile_differ", "differ.py")
    applier = _load("reconcile_applier", "applier.py")
    health_mod = _load("reconcile_health", "health.py")
    invariants_mod = _load("reconcile_invariants", "invariants.py")
    binding_store_mod = _load("reconcile_binding_store", "binding_store.py")
    outbound_differ_mod = _load("reconcile_outbound_differ", "outbound_differ.py")
    inbound_differ_mod = _load("reconcile_inbound_differ", "inbound_differ.py")
    local_label_intent_mod = _load("reconcile_local_label_intent", "local_label_intent.py")
    sync_logger_mod = _load("reconcile_sync_logger", "sync_logger.py")

    # -----------------------------------------------------------------------
    # Persistence gating (ticket yaw-plait-doe).
    #
    # cap-0 modes (dry-run, reconcile-check) are documented as read-only: they
    # run the full differ COMPUTATION and PRODUCE the report, but must write
    # NOTHING to the local store. Every write point below is gated on `persist`.
    #
    # target_mode None defaults to LIVE → persists. dry-run / reconcile-check
    # → cap 0 → persist=False. bootstrap-* / live → non-zero/None cap → persist.
    # -----------------------------------------------------------------------
    mode_mod = _load("rebar_reconciler.mode", "mode.py")
    if target_mode is None:
        persist = True
    else:
        persist = mode_mod.MODE_CAPS.get(target_mode) != 0

    # -----------------------------------------------------------------------
    # Sync logger: create at pass start, close at pass end (finally block).
    # In no-write mode use a no-op logger so no sync-log-<ts>.jsonl is created.
    # -----------------------------------------------------------------------
    log_path = repo_root / "bridge_state" / f"sync-log-{pass_id}.jsonl"
    sync_logger = sync_logger_mod.SyncLogger(log_path) if persist else _NoOpSyncLogger()
    sync_logger.log(
        "sync_pass_start",
        pass_id=pass_id,
        mode=target_mode.value if target_mode else "live",
        filtered=bool(filter_local_ids),
        filter_count=len(filter_local_ids) if filter_local_ids else 0,
    )
    if filter_local_ids:
        print(  # noqa: T201
            f"FILTERED PASS: scope restricted to {len(filter_local_ids)} "
            f"local IDs — not a production reconciliation."
        )

    # Ensure snapshots directory exists
    snapshots_dir = repo_root / "bridge_state" / "snapshots"
    snapshots_dir.mkdir(parents=True, exist_ok=True)

    # -----------------------------------------------------------------------
    # Read local tickets from the ticket CLI.
    # -----------------------------------------------------------------------
    local_tickets = _read_local_tickets(repo_root, no_sync=not persist)

    # -----------------------------------------------------------------------
    # Load and recover binding store.
    # -----------------------------------------------------------------------
    binding_store = binding_store_mod.load_binding_store(repo_root)

    # Read previous snapshot from the tickets-tracker directory (persisted  # tickets-boundary-ok
    # between GHA runs via the commit-back step). The earlier approach wrote
    # prev.json to bridge_state/snapshots/ on the main-branch worktree, but
    # that filesystem is ephemeral — every GHA run starts fresh, so
    # prev_snapshot was always {} and the differ re-derived all 2050+
    # inbound_create mutations on every pass. Writing to the tracker dir  # tickets-boundary-ok
    # ensures the snapshot survives between runs because the workflow's
    # commit-back step commits everything under that directory.  # tickets-boundary-ok
    tracker_dir = repo_root / ".tickets-tracker"  # tickets-boundary-ok
    prev_dir = tracker_dir / ".bridge_state"
    prev_dir.mkdir(parents=True, exist_ok=True)
    prev_path = prev_dir / "prev_snapshot.json"
    if prev_path.exists():
        try:
            prev_snapshot: dict = json.loads(prev_path.read_text())
        except (json.JSONDecodeError, ValueError, OSError) as _exc:
            # A corrupt / conflict-marked prev_snapshot must NEVER let the pass
            # proceed with an unknown Jira state — alert + abort (see helper).
            _handle_corrupt_snapshot(pass_id, repo_root, prev_path, _exc)
    else:
        prev_snapshot = {}

    # Fetch current remote state. In no-write mode use compute_snapshot so no
    # snapshot file is written; the differ runs identically on curr_snapshot.
    if persist:
        curr_path = fetcher.fetch_snapshot(pass_id, repo_root)
        curr_snapshot: dict = json.loads(curr_path.read_text())
    else:
        curr_path = None
        curr_snapshot = fetcher.compute_snapshot(pass_id, repo_root)

    ctx.persist = persist
    ctx.fetcher = fetcher
    ctx.differ = differ
    ctx.applier = applier
    ctx.health_mod = health_mod
    ctx.invariants_mod = invariants_mod
    ctx.binding_store_mod = binding_store_mod
    ctx.outbound_differ_mod = outbound_differ_mod
    ctx.inbound_differ_mod = inbound_differ_mod
    ctx.local_label_intent_mod = local_label_intent_mod
    ctx.sync_logger_mod = sync_logger_mod
    ctx.mode_mod = mode_mod
    ctx.sync_logger = sync_logger
    ctx.local_tickets = local_tickets
    ctx.binding_store = binding_store
    ctx.tracker_dir = tracker_dir
    ctx.prev_path = prev_path
    ctx.prev_snapshot = prev_snapshot
    ctx.curr_path = curr_path
    ctx.curr_snapshot = curr_snapshot


def _handle_corrupt_snapshot(
    pass_id: str, repo_root: Path, prev_path: Path, _exc: Exception
) -> None:
    """Abort the pass on a corrupt / conflict-marked ``prev_snapshot.json``.

    Lifted out of the ``reconcile_once`` spine (the corrupt-snapshot abort): emit
    a loud operator ERROR, best-effort record a critical alert, then raise
    ``RuntimeError``. The pass must NEVER proceed with an unknown Jira state, so
    this always raises.
    """
    # SAFETY INVARIANT: a corrupt or conflict-marked prev_snapshot.json
    # must NEVER cause the pass to proceed with an unknown Jira comment
    # state.  If we continued with prev_snapshot={}, the inbound differ
    # would re-derive all create mutations (expensive but safe).  However,
    # the outbound differ uses curr_snapshot (the live fetch), not
    # prev_snapshot, for comment dedup — so comment mutations would be
    # correct IF we could reach that point.  The problem is we cannot
    # trust that even prev_snapshot corruption is the only issue; the
    # tickets branch may be in a partially-merged state that makes curr
    # state unknown too.  Abort the pass with a loud ERROR and alert.
    _alert_key = f"corrupt_prev_snapshot:{pass_id}"
    print(  # noqa: T201
        f"ERROR: prev_snapshot.json is corrupt or contains git conflict "
        f"markers and cannot be parsed. Aborting reconcile pass "
        f"'{pass_id}' to prevent emitting mutations against unknown "
        f"Jira state. File: {prev_path}. Error: {_exc}. "
        f"Recovery: resolve the merge conflict or delete the file to "
        f"force a full re-fetch on the next pass.",
        file=sys.stderr,
    )
    try:
        _alert_store = _load(
            "rebar_reconciler.alert_store",
            "alert_store.py",
        )
        _alert_store.append(
            {
                "key": _alert_key,
                "severity": "critical",
                "reason": (f"prev_snapshot.json corrupt/unparseable at {prev_path}: {_exc}"),
                "pass_id": pass_id,
                "file": str(prev_path),
                "resolved": False,
                "timestamp_ns": __import__("time").time_ns(),
            },
            repo_root,
        )
    except Exception as _alert_exc:  # noqa: BLE001 — best-effort alert; original corruption still raises
        print(  # noqa: T201
            f"ERROR: alert_store write also failed ({_alert_exc}); "
            f"corruption event not persisted to bridge_alerts.",
            file=sys.stderr,
        )
    raise RuntimeError(
        f"Aborting reconcile pass '{pass_id}': prev_snapshot.json "
        f"is corrupt or contains git conflict markers at {prev_path}. "
        f"Original parse error: {_exc}. "
        f"Recovery: resolve the merge conflict or delete the file."
    ) from _exc


def _apply_mutations(ctx: _PassContext) -> None:
    """Apply phase: optional filter-scope narrowing + status preflight + the single
    applier.apply dispatch (wrapped so health.record_pass fires even on failure).
    Records manifest_path / nowrite_plan / the unfiltered count back onto ctx.
    """
    mutations = ctx.mutations
    filter_local_ids = ctx.filter_local_ids
    binding_store = ctx.binding_store
    pass_id = ctx.pass_id
    repo_root = ctx.repo_root
    target_mode = ctx.target_mode
    persist = ctx.persist
    applier = ctx.applier
    health_mod = ctx.health_mod
    sync_logger = ctx.sync_logger

    # Story 21dd: the reconciler's outbound apply publishes ticket writes externally
    # (and to Jira), so fail CLOSED on a store this rebar cannot interpret BEFORE any
    # mutation. Guarded by `persist` so dry-run / cap-0 preview passes (which write
    # nothing) are excluded. The reconciler resolves the store directly, so use the
    # `.tickets-tracker` boundary here — not config.tracker_dir().
    if persist:
        from rebar._store.compat import check_store_compat

        check_store_compat(repo_root / ".tickets-tracker")  # tickets-boundary-ok — Finding 2

    # -------------------------------------------------------------------
    # Post-filter: when filter_local_ids is set, discard mutations that
    # target tickets outside the filter scope.  All three differs ran on
    # their full, unfiltered inputs (same code paths as production); only
    # the dispatch set is narrowed.
    # -------------------------------------------------------------------
    unfiltered_count = len(mutations)
    if filter_local_ids:
        target_set = _build_filter_target_set(filter_local_ids, binding_store)
        mutations = [m for m in mutations if _mutation_matches_filter(m, target_set)]
        print(  # noqa: T201
            f"filter: {unfiltered_count} mutations computed, "
            f"{len(mutations)} match filter ({len(filter_local_ids)} local IDs, "
            f"{len(target_set)} target keys)",
            file=sys.stderr,
        )
        sync_logger.log(
            "filter_applied",
            unfiltered=unfiltered_count,
            filtered=len(mutations),
            target_keys=len(target_set),
        )

    # Preflight: WARN (non-fatally) if any update mutation references a status
    # not present in config.local_to_jira_status. Runs exactly once per pass,
    # before any applier dispatch. It no longer aborts the pass (Facet 3): an
    # unmapped status flows to the applier and is recorded there as a
    # per-mutation failure rather than taking down every later mutation.
    preflight_status_mapping(mutations)

    # F8: wrap apply in try/except/finally so health.record_pass STILL fires
    # on apply failure with degraded fields (local_mutation_count=0,
    # failure_kind set). Without this wrapping, failed passes were invisible
    # to monitoring.
    #
    # Direction-aware dispatch lives inside applier.apply (PR #371 / defect
    # #8): the applier partitions typed Mutations by direction internally and
    # routes inbound via _apply_typed per-mutation, outbound via the batch
    # path. The previous reconcile_once-level typed/legacy split (commit
    # cb858e468d) was a parallel workaround for the same gap; with cap
    # enforcement landing in applier.apply (story 286b), all mutations must
    # flow through that single entry point so caps apply uniformly across
    # both directions.
    manifest_path = None
    nowrite_plan: dict | None = None
    apply_exc: BaseException | None = None
    try:
        # Backward compatibility: tests stub applier.apply with a signature
        # that does not accept the `mode` kwarg. Only pass it when caller
        # actually supplied a target_mode (i.e., when cap enforcement is
        # requested).
        # Only forward abort_check when set, so tests that stub applier.apply with
        # a narrower signature are unaffected (epic dust-troth-naval).
        _abort_kw = {"abort_check": ctx.abort_check} if ctx.abort_check is not None else {}
        if target_mode is None:
            manifest_path = applier.apply(
                mutations, pass_id, repo_root, binding_store=binding_store, **_abort_kw
            )
        else:
            manifest_path = applier.apply(
                mutations,
                pass_id,
                repo_root,
                mode=target_mode,
                binding_store=binding_store,
                persist=persist,
                **_abort_kw,
            )
    except BaseException as exc:  # noqa: BLE001 — must re-raise after recording
        apply_exc = exc
        raise
    finally:
        # In no-write mode, apply() returns the computed plan dict instead of
        # a manifest Path. Capture it for the report and treat manifest_path
        # as None so no on-disk manifest is expected by the tally below.
        if not persist and isinstance(manifest_path, dict):
            nowrite_plan = manifest_path
            manifest_path = None
        # health.record_pass writes a bridge_state/health/<ts>.json file —
        # skip it in no-write mode (ticket yaw-plait-doe).
        if persist:
            per_type_counts = health_mod.count_open_by_type(repo_root=repo_root)
            if apply_exc is None:
                health_mod.record_pass(
                    pass_id=pass_id,
                    pre_fsck=0,
                    post_fsck=0,
                    per_type_counts=per_type_counts,
                    local_mutation_count=len(mutations),
                    repo_root=repo_root,
                )
            else:
                # Classify the failure: reschedule vs generic apply error.
                failure_kind = (
                    "reschedule" if type(apply_exc).__name__ == "RescheduleError" else "apply_error"
                )
                health_mod.record_pass(
                    pass_id=pass_id,
                    pre_fsck=0,
                    post_fsck=0,
                    per_type_counts=per_type_counts,
                    local_mutation_count=0,
                    repo_root=repo_root,
                    failure_kind=failure_kind,
                )

    ctx.mutations = mutations
    ctx.unfiltered_count = unfiltered_count
    ctx.manifest_path = manifest_path
    ctx.nowrite_plan = nowrite_plan


def _advance_baselines(binding_store: Any, curr_snapshot: Mapping[str, Any]) -> int:
    """Advance every CONFIRMED binding's per-binding baseline to the current snapshot
    (story d6bd — the always-on successor to the retired dual-write shadow). Only
    confirmed bindings whose Jira key is in the current fetch window are advanced (an
    out-of-window key has no fresh value this pass); ``set_baseline`` filters to the
    mirrored fields. In-memory until the caller's ``save()`` persists them (ADR 0026).
    """
    advanced = 0
    for local_id, entry in binding_store.all_bindings().items():
        if entry.get("state") != "confirmed":
            continue
        jira_key = entry.get("jira_key")
        if not jira_key or jira_key not in curr_snapshot:
            continue
        binding_store.set_baseline(local_id, curr_snapshot[jira_key])
        advanced += 1
    return advanced


def _persist_and_log(ctx: _PassContext) -> dict:
    """Persist phase: save+commit the binding store, advance the prev snapshot
    (idempotency), tally the truthful applied/failure counts from the manifest,
    close the sync logger, and assemble the result dict.
    """
    persist = ctx.persist
    binding_store = ctx.binding_store
    repo_root = ctx.repo_root
    curr_path = ctx.curr_path
    prev_path = ctx.prev_path
    manifest_path = ctx.manifest_path
    nowrite_plan = ctx.nowrite_plan
    mutations = ctx.mutations
    pass_id = ctx.pass_id
    sync_logger = ctx.sync_logger
    target_mode = ctx.target_mode
    filter_local_ids = ctx.filter_local_ids
    unfiltered_count = ctx.unfiltered_count

    # -------------------------------------------------------------------
    # Post-apply: save binding store, advance snapshot, close sync logger.
    # -------------------------------------------------------------------
    # binding_store.save() writes .bridge_state/bindings.json; the commit
    # helper writes/commits it to the tickets branch. Both are store writes —
    # skip the entire block in no-write mode (ticket yaw-plait-doe).
    if persist:
        # Convergence rollout retired (story d6bd): ALWAYS advance the per-binding
        # baselines from the current snapshot (formerly gated on the removed
        # reconciler.baseline_dual_write). This records the last-synced Jira-side
        # ancestor the outbound field differ arbitrates against (ADR 0026). Runs
        # BEFORE save() so they persist this pass; fail-open (never break a sync pass).
        try:
            _advance_baselines(binding_store, ctx.curr_snapshot)
        except Exception as exc:  # noqa: BLE001 — baseline advance is best-effort; never break sync
            print(  # noqa: T201
                f"reconcile: baseline advance failed ({exc})",
                file=sys.stderr,
            )
        try:
            binding_store.save()
            # Commit the updated bindings.json to the tickets orphan branch so
            # it survives a concurrent ``git merge origin/tickets`` in the
            # ticket-CLI's _push_tickets_branch() between reconciler passes.
            # Without this commit, local probe runs lose newly-created bindings
            # on the next ticket-CLI push, causing the next reconciler pass to
            # see bound tickets as unbound and generate CREATE rather than
            # UPDATE mutations (regression: outbound scalar edits never land).
            if not _commit_binding_store_snapshot(binding_store, repo_root, pass_id):
                # Commit failed — bindings are on disk but NOT on the tickets
                # branch. A concurrent ``git merge origin/tickets`` between now
                # and the next pass can clobber the working-tree bindings.json
                # with the remote version, making bound tickets appear unbound
                # (cf93b2b7ad class). _commit_binding_store_snapshot already
                # logged the error and filed the alert. Do NOT abort the pass —
                # commit failure must never break sync.
                print(  # noqa: T201
                    "ERROR: reconcile: binding-store commit to tickets branch failed; "
                    "bindings are at risk of clobber on the next 'git merge origin/tickets'. "
                    "The current pass will complete normally. Check git state in "
                    ".tickets-tracker and ensure the GHA commit-back step runs to persist "
                    "bindings before the next reconciler pass.",
                    file=sys.stderr,
                )
        except Exception as exc:  # noqa: BLE001 — fail-open: save failure must never break sync, log only
            print(  # noqa: T201
                f"reconcile: binding store save failed ({exc})",
                file=sys.stderr,
            )

        # Advance prev snapshot so the next call converges to zero mutations.
        # Only in persist mode: curr_path is None in no-write mode and the
        # prev_snapshot must stay untouched (no store write). Both paths are
        # guaranteed set by _load_snapshots in persist mode (the invariant the
        # surrounding ``if persist`` encodes) — assert it to narrow the Optional.
        assert curr_path is not None and prev_path is not None
        shutil.copy2(curr_path, prev_path)

    # Bug 85a1: surface the truthful applied-count and failure-count by parsing
    # the manifest written by _apply_batch. Before this fix, sync_pass_end and
    # the result dict reported mutations_applied=len(mutations) — the COMPUTED
    # count, not the count that actually reached a handler. The "OK: converged"
    # message in __main__ inherited that lie.
    #
    # Semantics: a manifest outcome with no "error" key counts as applied (the
    # handler ran without raising — even update_one's comment-fallback path that
    # returns result=None on 400 illegal-transition counts as applied because a
    # comment was added). An outcome with an "error" key counts as a failure.
    #
    # Degrades gracefully: if manifest_path is None (rare paths), or the JSON
    # cannot be parsed, the counts conservatively default to (mutation_count, 0)
    # so existing callers reading mutations_applied receive a number consistent
    # with the prior contract.
    # No-write (cap-0) mode: nothing is applied, so the tally is (0, 0) and the
    # computed plan comes from the in-memory rendered dict (no manifest file).
    mutations_applied = len(mutations)
    mutation_failures = 0
    if nowrite_plan is not None:
        mutations_applied = 0
        mutation_failures = 0
    elif manifest_path is not None:
        try:
            manifest_data = json.loads(Path(manifest_path).read_text())
            # Two manifest shapes coexist (bug 85a1 follow-up):
            #   1. Legacy/LIVE — written by _apply_batch with a flat
            #      ``mutations`` list of outcome dicts; each outcome with no
            #      ``error`` key counts as applied.
            #   2. Asymmetric/BOOTSTRAP — written by manifest_renderer when
            #      mode caps are in effect (bootstrap-strict/throttle/dry-run).
            #      Carries an explicit ``applied_count`` integer and direction
            #      totals; no flat ``mutations`` list.
            # Detect the asymmetric shape via the presence of ``applied_count``
            # and prefer it when present (it's the authoritative apply tally).
            # Otherwise fall back to the legacy outcomes-list count.
            if "applied_count" in manifest_data:
                mutations_applied = int(manifest_data["applied_count"])
                mutation_failures = int(manifest_data.get("failed_count", 0))
            else:
                outcomes = manifest_data.get("mutations", []) or []
                mutations_applied = sum(1 for o in outcomes if not o.get("error"))
                mutation_failures = sum(1 for o in outcomes if o.get("error"))
        except Exception as exc:  # noqa: BLE001 — fail-open: fall back to computed count, log only
            print(  # noqa: T201
                f"reconcile: manifest tally read failed ({exc}) — falling back to computed count",
                file=sys.stderr,
            )

    # Story 9622: pending-binding recovery failures (set by run_differs on ctx) are
    # surfaced as a tally — observability-only, NOT an exit gate (recovery is
    # best-effort/fail-open; a transient Jira search hiccup must not fail the pass).
    recovery_failures = int(getattr(ctx, "recovery_failures", 0) or 0)

    sync_logger.log(
        "sync_pass_end",
        pass_id=pass_id,
        mutations_computed=len(mutations),
        mutations_applied=mutations_applied,
        mutation_failures=mutation_failures,
        recovery_failures=recovery_failures,
    )
    sync_logger.close()

    result = {
        "pass_id": pass_id,
        "mutation_count": len(mutations),
        "mutations_applied": mutations_applied,
        "mutation_failures": mutation_failures,
        "recovery_failures": recovery_failures,
        "manifest_path": str(manifest_path) if manifest_path is not None else None,
    }
    # No-write (cap-0) mode: surface the COMPUTED plan in the result so callers
    # (rebar.reconcile / MCP) receive the detailed mutation plan even though no
    # manifest file was written (ticket yaw-plait-doe).
    if nowrite_plan is not None:
        result["no_write"] = True
        result["mode"] = getattr(target_mode, "value", str(target_mode))
        result["plan"] = _build_plan_entries(mutations)
    if filter_local_ids:
        result["filtered"] = True
        result["filter_local_ids"] = sorted(filter_local_ids)
        result["unfiltered_mutation_count"] = unfiltered_count
    return result
