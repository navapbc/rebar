#!/usr/bin/env python3
"""reconcile.py — one-pass orchestrator: fetch → diff → apply.

reconcile_once(pass_id) wires the three reconciler stages into a single
idempotent pass.  Two consecutive calls with an unchanged remote produce
mutation_count=0 on both passes (second call sees prev==curr snapshot).
"""

from __future__ import annotations

import importlib.util
import json
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
        _loader_spec.loader.exec_module(_loader_mod)
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
# preflight, binding-store commit-back, the ticket-CLI reader, the filter-scope
# builders, the no-write plan renderer, and the cap-0 sync-logger stand-in). Load
# it once by path and bind its names at module level so (a) the staying phase
# helpers call them as bare names — preserving the monkeypatch seam tests rely on —
# and (b) attribute access (``reconcile.<name>``, used by tests that load this
# module by path) keeps resolving all eight names.
# ---------------------------------------------------------------------------
_helpers = _load("reconcile_helpers", "reconcile_helpers.py")

StatusMappingError = _helpers.StatusMappingError
preflight_status_mapping = _helpers.preflight_status_mapping
_commit_binding_store_snapshot = _helpers._commit_binding_store_snapshot
_read_local_tickets = _helpers._read_local_tickets
SelectionStaleError = _helpers.SelectionStaleError
ensure_selection_current = _helpers.ensure_selection_current
narrow_selection_inputs = _helpers.narrow_selection_inputs
_build_filter_target_set = _helpers._build_filter_target_set
_mutation_matches_filter = _helpers._mutation_matches_filter
_build_plan_entries = _helpers._build_plan_entries
_NoOpSyncLogger = _helpers._NoOpSyncLogger
# ADR-0026 baseline advance (bug e6e9 grew it past the module-size cap). Pure helpers over
# the binding store with no back-edge to the reconcile_once spine — exactly what
# reconcile_helpers holds — re-bound here so the bare-name calls in _persist_and_log and
# the ``reconcile._advance_baselines`` import in the A3 oracle both keep resolving.
_accepts_synced_fields_out = _helpers._accepts_synced_fields_out
_accepts_client = _helpers._accepts_client
_advance_baselines = _helpers._advance_baselines
_advance_peer_parent = _helpers._advance_peer_parent
# RP-04 S3 (AC1/AC6): the runtime-binding cluster lives in reconcile_helpers (no back-edge
# to this spine); re-bound here so reconcile_once calls them as bare names and tests that
# load this module by path can reach them as ``reconcile.<name>``.
_write_facade_enabled = _helpers._write_facade_enabled
_resolve_pass_transport = _helpers._resolve_pass_transport
bind_operation_runtime = _helpers.bind_operation_runtime

# RP-04 S3 (AC1): the reconcile operation runtime (S2). Bound as a MODULE attribute
# ``reconcile.compose_reconciler_runtime`` so a pass composes ONE runtime whose backend
# CAPTURES scope at compose time (no ambient re-resolution per apply), and so tests can
# monkeypatch this exact attribute. Loaded by the same by-path sibling loader the rest of
# this module uses (runtime.py resolves its heavy deps lazily) so it resolves whether or not
# the engine dir is importable as a package.
compose_reconciler_runtime = _load(
    "rebar_reconciler.runtime", "runtime.py"
).compose_reconciler_runtime


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
    selection_kind: str | None = None
    selection_ids: set[str] | None = None
    max_changes: int | None = None
    route: str | None = None
    # optional per-mutation lost-lease checkpoint (epic dust-troth-naval): a
    # zero-arg callable the applier invokes before each mutation; it raises
    # (ReconcileLockLost) if the ref-lock heartbeat lost the lease. None = no-op.
    abort_check: Any = None
    # populated by _load_snapshots
    persist: bool = True
    fetcher: Any = None
    differ: Any = None
    applier: Any = None
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
    # LIVE applied/failed counts read out of the manifest before it was unlinked
    # (bug c903) — LIVE leaves no manifest file, so this is the only failure record.
    apply_tally: dict | None = None
    # local_id -> {vendor_field: value} for the outbound writes CONFIRMED landed this pass
    # (bug e6e9), so _advance_baselines records the last-SYNCED value, not the pass-start
    # fetch. Empty degrades that advance to its pre-e6e9 fetch-only behaviour exactly.
    synced_fields: dict[str, dict] = field(default_factory=dict)
    # RP-04 S3 (AC1): the composed operation runtime's already-built backend and its
    # captured transport, threaded into _apply_mutations so the apply phase forwards the
    # transport as applier.apply(client=...) instead of re-resolving config ambiently via
    # _load_acli. None when the write facade is disabled (AC6) or composition was skipped
    # for a no-write pass whose scope is absent.
    runtime_backend: Any = None
    runtime_transport: Any = None


def reconcile_once(
    pass_id: str,
    repo_root: Path | None = None,
    target_mode=None,
    filter_local_ids: set[str] | None = None,
    selection_kind: str | None = None,
    selection_ids: set[str] | None = None,
    max_changes: int | None = None,
    route: str | None = None,
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
        from rebar.config import reconciler_repo_root as _owned_repo_root

        repo_root = _owned_repo_root()
    ctx = _PassContext(
        pass_id=pass_id,
        repo_root=repo_root,
        target_mode=target_mode,
        filter_local_ids=filter_local_ids,
        route=route,
        selection_kind=selection_kind,
        selection_ids=selection_ids,
        max_changes=max_changes,
        abort_check=abort_check,
    )
    _load_snapshots(ctx)
    # RP-04 S3 (AC1): compose the ONE operation runtime for this pass and thread its
    # already-built backend's transport into the apply phase (AC6 toggle honored inside).
    # Pass the module-level ``compose_reconciler_runtime`` seam (monkeypatched by tests)
    # so the helper stays free of a back-edge to this spine.
    bind_operation_runtime(ctx, compose_reconciler_runtime)
    # Diff phase lives in the sibling run_differs.py (loaded lazily by file path,
    # matching the sibling-loader convention). It holds no back-edge to reconcile.py.
    run_differs_mod = _load("reconcile_run_differs", "run_differs.py")
    run_differs_mod.run_differs(ctx)
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
    selection_ids = ctx.selection_ids
    fetcher = _load("reconcile_fetcher", "fetcher.py")
    differ = _load("reconcile_differ", "differ.py")
    applier = _load("reconcile_applier", "applier.py")
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
    # Read local tickets from the ticket CLI.
    # -----------------------------------------------------------------------
    local_tickets = _read_local_tickets(repo_root, no_sync=(not persist) or bool(selection_ids))

    # -----------------------------------------------------------------------
    # Load and recover binding store.
    # -----------------------------------------------------------------------
    binding_store = binding_store_mod.load_binding_store(repo_root)
    if selection_ids:
        ensure_selection_current(selection_ids, local_tickets)

    # Create write-bearing pass artifacts only after the under-lock staleness check.
    log_path = repo_root / "bridge_state" / f"sync-log-{pass_id}.jsonl"
    sync_logger = sync_logger_mod.SyncLogger(log_path) if persist else _NoOpSyncLogger()
    scoped_ids = selection_ids or filter_local_ids
    sync_logger.log(
        "sync_pass_start",
        pass_id=pass_id,
        mode=target_mode.value if target_mode else "live",
        filtered=bool(scoped_ids),
        filter_count=len(scoped_ids) if scoped_ids else 0,
    )
    # Interrupted-retirement repair (RP-02 S3 T2): after the under-lock staleness gate,
    # and BEFORE the first remote fetch below — so no pass can interleave fresh liveness
    # evidence for a retired issue with completing that issue's retirement.
    binding_store.repair_at_write_boundary(persist=persist, scoped=bool(scoped_ids))
    if scoped_ids:
        print(
            f"FILTERED PASS: scope restricted to {len(scoped_ids)} "
            f"local IDs — not a production reconciliation."
        )

    snapshots_dir = repo_root / "bridge_state" / "snapshots"
    snapshots_dir.mkdir(parents=True, exist_ok=True)

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

    if selection_ids and ctx.selection_kind:
        local_tickets, prev_snapshot, curr_snapshot = narrow_selection_inputs(
            ctx.selection_kind,
            selection_ids,
            local_tickets,
            prev_snapshot,
            curr_snapshot,
            binding_store,
        )

    ctx.persist = persist
    ctx.fetcher = fetcher
    ctx.differ = differ
    ctx.applier = applier
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
    print(
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
        print(
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
    applier.apply dispatch and normalize its write/no-write return shapes.
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
        print(
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

    # Direction-aware dispatch lives inside applier.apply (PR #371 / defect
    # #8): the applier partitions typed Mutations by direction internally and
    # routes inbound via _apply_typed per-mutation, outbound via the batch
    # path. The previous reconcile_once-level typed/legacy split was a
    # parallel workaround for the same gap; with cap
    # enforcement landing in applier.apply (story 286b), all mutations must
    # flow through that single entry point so caps apply uniformly across
    # both directions.
    manifest_path = None
    nowrite_plan: dict | None = None
    # Bug c903: LIVE returns its applied/failed tally here instead of a Path.
    apply_tally: dict | None = None
    try:
        # Backward compatibility: tests stub applier.apply with a signature
        # that does not accept the `mode` kwarg. Only pass it when caller
        # actually supplied a target_mode (i.e., when cap enforcement is
        # requested).
        # Only forward abort_check when set, so tests that stub applier.apply with
        # a narrower signature are unaffected (epic dust-troth-naval).
        _abort_kw = {"abort_check": ctx.abort_check} if ctx.abort_check is not None else {}
        # Bug e6e9: forwarded ONLY when the resolved applier accepts it, mirroring the
        # narrow-signature tolerance the abort_check kwarg above already needs — tests
        # stub applier.apply with fixed signatures, and an unexpected kwarg would turn a
        # baseline refinement into a TypeError that aborts the pass.
        _synced_kw = (
            {"synced_fields_out": ctx.synced_fields}
            if _accepts_synced_fields_out(applier.apply)
            else {}
        )
        # AC1: forward the composed runtime's captured transport as client= so the applier
        # skips ambient _load_acli. Forwarded ONLY when present (compose succeeded / facade
        # ON) AND the resolved applier accepts it, mirroring the narrow-signature tolerance
        # above so a stubbed applier.apply is never handed an unexpected kwarg.
        _client_kw = (
            {"client": ctx.runtime_transport}
            if ctx.runtime_transport is not None and _accepts_client(applier.apply)
            else {}
        )
        if target_mode is None:
            manifest_path = applier.apply(
                mutations,
                pass_id,
                repo_root,
                binding_store=binding_store,
                **_abort_kw,
                **_synced_kw,
                **_client_kw,
            )
        else:
            _max_kw = {"max_changes": ctx.max_changes} if ctx.max_changes is not None else {}
            _route_kw = {"route": ctx.route} if ctx.route is not None else {}
            manifest_path = applier.apply(
                mutations,
                pass_id,
                repo_root,
                mode=target_mode,
                binding_store=binding_store,
                persist=persist,
                **_max_kw,
                **_route_kw,
                **_abort_kw,
                **_synced_kw,
                **_client_kw,
            )
    finally:
        # In no-write mode, apply() returns the computed plan dict instead of
        # a manifest Path. Capture it for the report and treat manifest_path
        # as None so no on-disk manifest is expected by the tally below.
        if not persist and isinstance(manifest_path, dict):
            nowrite_plan = manifest_path
            manifest_path = None
        # Bug c903: in LIVE (persist=True) apply() returns the applied/failed tally
        # read out of the manifest just before it was unlinked, NOT a Path. Route it
        # to ctx.apply_tally so _persist_and_log can count failures without an
        # on-disk manifest. Discriminated from the no-write plan dict above by
        # `persist`, which is False there and True here.
        elif persist and isinstance(manifest_path, dict):
            apply_tally = manifest_path
            manifest_path = None

    ctx.mutations = mutations
    ctx.unfiltered_count = unfiltered_count
    ctx.manifest_path = manifest_path
    ctx.nowrite_plan = nowrite_plan
    ctx.apply_tally = apply_tally


def _confirm_peer_links(ctx: _PassContext, pass_id: str) -> int:
    """Record peer-confirmation evidence from this pass's snapshot (epic a4bd).

    Kept as a named seam rather than inlined so the persist phase reads as a list of
    steps and so tests can drive it directly. Opening the store here (not once per
    pass elsewhere) keeps the whole feature inside the ``persist`` branch: a no-write
    pass must not write evidence any more than it writes bindings.

    ONE STORE INSTANCE FOR BOTH HALVES (epic a4bd, story f6e9). The upgrade backfill
    and the snapshot confirmation MUST share a single instance. Two instances would
    each load a pre-write copy of the file, so (a) records backfilled this pass would
    be invisible to snapshot confirmation, defeating the same-pass provenance upgrade,
    and (b) — worse — whichever instance saved last would silently discard the other's
    records: a lost update. Sharing one instance makes the upgrade fall out of plain
    in-memory ordering, since snapshot ``record()`` overwrites the backfilled entry
    before anything is written to disk.

    Backfill runs FIRST for that reason, and saving happens ONCE at the end.
    """
    from rebar_reconciler.peer_confirmations import (
        backfill_from_managed_refs,
        confirm_from_snapshot,
        open_store,
    )

    store = open_store(ctx.repo_root)
    written = backfill_from_managed_refs(store, ctx.local_tickets, ctx.binding_store, pass_id)
    written += confirm_from_snapshot(store, ctx.curr_snapshot, ctx.binding_store, pass_id)
    if written:
        store.save()
    return written


def _write_prev_snapshot_key_set(prev_path: Path, curr_snapshot: Mapping[str, Any]) -> None:
    """Persist only Jira-key membership for the next pass's edge detection."""
    key_set: dict[str, dict[str, Any]] = {jira_key: {} for jira_key in sorted(curr_snapshot)}
    prev_path.write_text(json.dumps(key_set, separators=(",", ":")) + "\n")


def _persist_and_log(ctx: _PassContext) -> dict:
    """Persist phase: save+commit the binding store, advance the prev snapshot
    (idempotency), tally the truthful applied/failure counts from the manifest,
    close the sync logger, and assemble the result dict.
    """
    persist = ctx.persist
    binding_store = ctx.binding_store
    repo_root = ctx.repo_root
    prev_path = ctx.prev_path
    manifest_path = ctx.manifest_path
    nowrite_plan = ctx.nowrite_plan
    apply_tally = ctx.apply_tally
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
            _advance_baselines(binding_store, ctx.curr_snapshot, ctx.synced_fields)
        except Exception as exc:  # noqa: BLE001 — baseline advance is best-effort; never break sync
            print(
                f"reconcile: baseline advance failed ({exc})",
                file=sys.stderr,
            )
        # Epic a4bd: learn peer-link evidence from THIS pass's authoritative fetch,
        # so a link the peer carries is provably synchronized even when this clone
        # never pushed it. Sits beside the baseline advance because it is the same
        # kind of step — "record what the current snapshot proves" — and runs before
        # save() for the same reason. Fail-open: losing evidence costs safety on a
        # later removal, whereas raising would break a sync pass that succeeded.
        try:
            _confirm_peer_links(ctx, pass_id)
        except Exception as exc:  # noqa: BLE001 — evidence is best-effort; never break sync
            print(
                f"reconcile: peer-link confirmation failed ({exc})",
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
                # (the clobbered-bindings class that
                # test_commit_binding_store_failure.py pins). The helper already
                # logged the error and filed the alert. Do NOT abort the pass —
                # commit failure must never break sync.
                print(
                    "ERROR: reconcile: binding-store commit to tickets branch failed; "
                    "bindings are at risk of clobber on the next 'git merge origin/tickets'. "
                    "The current pass will complete normally. Check git state in "
                    ".tickets-tracker and ensure the GHA commit-back step runs to persist "
                    "bindings before the next reconciler pass.",
                    file=sys.stderr,
                )
        except Exception as exc:  # noqa: BLE001 — fail-open: save failure must never break sync, log only
            print(
                f"reconcile: binding store save failed ({exc})",
                file=sys.stderr,
            )

        # Advance only Jira-key membership so the next call preserves edge detection
        # without retaining remote field bodies. The full current snapshot remains
        # available as the per-pass diagnostic artifact under bridge_state/snapshots/.
        # In no-write mode prev_path must stay untouched, so this remains inside the
        # persistence gate.
        assert prev_path is not None
        _write_prev_snapshot_key_set(prev_path, ctx.curr_snapshot)

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
    elif apply_tally is not None:
        # Bug c903: LIVE deletes its manifest, so the counts arrive out-of-band from
        # _emit_mode_manifest (read immediately before the unlink). Without this branch
        # the tally fell through to the (len(mutations), 0) default below, which made
        # `mutation_failures` structurally 0 in the only mode production runs — leaving
        # __main__'s `if failures > 0: return 1` unreachable and printing "applied N of
        # N" while mutations failed.
        mutations_applied = int(apply_tally.get("applied_count", 0))
        mutation_failures = int(apply_tally.get("failed_count", 0))
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
            print(
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
        if ctx.route == "preview":
            result.update(nowrite_plan)
        else:
            result["mode"] = getattr(target_mode, "value", str(target_mode))
            result["plan"] = _build_plan_entries(mutations)
    if filter_local_ids:
        result["filtered"] = True
        result["filter_local_ids"] = sorted(filter_local_ids)
        result["unfiltered_mutation_count"] = unfiltered_count
    return result
