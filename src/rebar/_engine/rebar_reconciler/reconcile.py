#!/usr/bin/env python3
"""reconcile.py — one-pass orchestrator: fetch → diff → apply.

reconcile_once(pass_id) wires the three reconciler stages into a single
idempotent pass.  Two consecutive calls with an unchanged remote produce
mutation_count=0 on both passes (second call sees prev==curr snapshot).
"""

from __future__ import annotations

import importlib.util
import sys
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


def _tracker_dir(repo_root: Path) -> Path:
    """The RELOCATABLE store, RESOLVED: env > ``tracker.dir`` > default under the root."""
    from rebar.config import tracker_dir

    return tracker_dir(repo_root)


# ---------------------------------------------------------------------------
# Leaf-helper re-exports. reconcile_helpers.py and its sibling pass_support.py
# (ticket piscine-bullish-cowbird split reconcile_helpers.py to restore its own
# module-size headroom) hold the pure pass-support utilities that carry no
# back-edge to the reconcile_once spine — status preflight, binding-store
# commit-back, the ticket-CLI reader, the selection/filter-scope builders (now
# pass_support.py), and the no-write plan renderer + cap-0 sync-logger stand-in
# (still reconcile_helpers.py). Load both once by path and bind their names at
# module level so (a) the staying phase helpers call them as bare names —
# preserving the monkeypatch seam tests rely on — and (b) attribute access
# (``reconcile.<name>``, used by tests that load this module by path) keeps
# resolving every name regardless of which sibling file now defines it.
# ---------------------------------------------------------------------------
_helpers = _load("reconcile_helpers", "reconcile_helpers.py")
_pass_support = _load("pass_support", "pass_support.py")

StatusMappingError = _pass_support.StatusMappingError
preflight_status_mapping = _pass_support.preflight_status_mapping
_commit_binding_store_snapshot = _pass_support._commit_binding_store_snapshot
_read_local_tickets = _pass_support._read_local_tickets
SelectionStaleError = _pass_support.SelectionStaleError
ensure_selection_current = _pass_support.ensure_selection_current
narrow_selection_inputs = _pass_support.narrow_selection_inputs
_build_filter_target_set = _pass_support._build_filter_target_set
_mutation_matches_filter = _pass_support._mutation_matches_filter
_build_plan_entries = _helpers._build_plan_entries
_NoOpSyncLogger = _helpers._NoOpSyncLogger
_write_prev_snapshot_key_set = _helpers._write_prev_snapshot_key_set
# ADR-0026 baseline advance (bug e6e9 grew it past the module-size cap). Pure helpers over
# the binding store with no back-edge to the reconcile_once spine — exactly what
# reconcile_helpers holds — re-bound here so the bare-name calls in _persist_and_log and
# the ``reconcile._advance_baselines`` import in the A3 oracle both keep resolving.
_accepts_synced_fields_out = _helpers._accepts_synced_fields_out
_accepts_client = _helpers._accepts_client
_accepts_ticket_plans = _helpers._accepts_ticket_plans
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


# ---------------------------------------------------------------------------
# Phase-function seams. reconcile_once's three write-bearing phases (load,
# apply, persist) moved to sibling modules load_phase.py / apply_phase.py /
# persist_phase.py (ticket piscine-bullish-cowbird: reconcile.py was at the
# locked 800-line cap). reconcile_once itself is untouched above — it remains
# the single lifecycle facade calling these four names, exactly as before.
#
# _apply_mutations and _confirm_peer_links are simple alias-binds: no test
# monkeypatches either function's OWN sub-dependencies through
# ``reconcile.<name>`` expecting the patch to alter that function's behavior,
# so the implementation can live entirely in its new sibling module.
#
# _load_snapshots, _save_and_commit_bindings, and _persist_and_log are thin
# WRAPPERS instead: existing tests patch collaborators these functions call
# internally — e.g. ``monkeypatch.setattr(reconcile, "_read_local_tickets",
# ...)`` and expect ``reconcile._load_snapshots`` to observe it — and a bare
# name resolves against its DEFINING module's globals, not the caller's. Each
# wrapper below resolves every patchable collaborator as a bare name in ITS
# OWN body (so it reads reconcile.py's current globals, picking up any
# monkeypatch applied before the call) and forwards it into the real
# implementation as an explicit keyword argument. This preserves every
# existing patch seam unchanged while the implementation itself lives outside
# this file.
# ---------------------------------------------------------------------------
_load_phase = _load("reconcile_load_phase", "load_phase.py")
_apply_phase = _load("reconcile_apply_phase", "apply_phase.py")
_persist_phase = _load("reconcile_persist_phase", "persist_phase.py")

_apply_mutations = _apply_phase.apply_mutations
_confirm_peer_links = _persist_phase.confirm_peer_links


def _load_snapshots(ctx: _PassContext) -> None:
    """Load phase: sibling modules + persist flag + sync logger + local tickets +
    binding store + the prev/curr snapshots (aborting via _handle_corrupt_snapshot
    on a corrupt prev_snapshot). Populates ctx for the diff/apply/persist phases.
    """
    _load_phase.load_snapshots(
        ctx,
        load=_load,
        read_local_tickets=_read_local_tickets,
        ensure_selection_current=ensure_selection_current,
        narrow_selection_inputs=narrow_selection_inputs,
        no_op_sync_logger_cls=_NoOpSyncLogger,
        handle_corrupt_snapshot=_handle_corrupt_snapshot,
    )


def _handle_corrupt_snapshot(
    pass_id: str, repo_root: Path, prev_path: Path, _exc: Exception
) -> None:
    """Abort the pass on a corrupt / conflict-marked ``prev_snapshot.json``.

    Lifted out of the ``reconcile_once`` spine (the corrupt-snapshot abort): emit
    a loud operator ERROR, best-effort record a critical alert, then raise
    ``RuntimeError``. The pass must NEVER proceed with an unknown Jira state, so
    this always raises.
    """
    return _load_phase.handle_corrupt_snapshot(pass_id, repo_root, prev_path, _exc)


def _save_and_commit_bindings(ctx: _PassContext) -> None:
    """Post-apply persistence for a writing pass (extracted from ``_persist_and_log``).

    Advances the per-binding baselines and peer-link evidence from THIS pass's proven
    snapshot, saves + commits the binding store to the tickets branch, runs the
    comment-id recording invariant, and advances the prev-snapshot key set. Every step is
    fail-open — a store-write hiccup must never break a pass that otherwise succeeded — and
    the whole block runs ONLY under the persistence gate (skipped in no-write mode).
    """
    _persist_phase.save_and_commit_bindings(
        ctx,
        commit_binding_store_snapshot=_commit_binding_store_snapshot,
        confirm_peer_links=_confirm_peer_links,
    )


def _persist_and_log(ctx: _PassContext) -> dict:
    """Persist phase: save+commit the binding store, advance the prev snapshot
    (idempotency), tally the truthful applied/failure counts from the manifest,
    close the sync logger, and assemble the result dict.
    """
    return _persist_phase.persist_and_log(
        ctx,
        save_and_commit_bindings=_save_and_commit_bindings,
    )
