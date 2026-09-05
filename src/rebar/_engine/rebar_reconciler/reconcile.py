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
# Canonical leaf-helper modules.
#
# ADR 0111 forbids preserving old private compatibility bindings after a
# move. Keep the sibling modules loaded once for the spine, but route every
# moved collaborator through its canonical owner at call time instead of
# rebinding it as ``reconcile.<name>``. The remaining names defined in this
# module (``_load``, ``_load_snapshots``, ``_persist_and_log``) are phase seams
# owned by this spine, not compatibility re-exports.
# ---------------------------------------------------------------------------
_helpers = _load("rebar_reconciler.reconcile_helpers", "reconcile_helpers.py")
_pass_support = _load("rebar_reconciler.pass_support", "pass_support.py")
_runtime = _load("rebar_reconciler.runtime", "runtime.py")


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
    # Compose the one operation runtime through the canonical runtime module at call time
    # so tests and callers patch the real owner, not an old ``reconcile.<name>`` shim.
    _helpers.bind_operation_runtime(ctx, _runtime.compose_reconciler_runtime)
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
# _apply_mutations and _confirm_peer_links remain direct phase seams owned by this
# spine: external guard tests call them to exercise the extracted phase functions.
#
# _load_snapshots, _save_and_commit_bindings, and _persist_and_log are thin
# wrappers around the sibling phase implementations. Their moved helper/runtime
# collaborators are resolved from canonical owner modules at call time rather than
# through old ``reconcile.<name>`` compatibility bindings.
# ---------------------------------------------------------------------------
_load_phase = _load("rebar_reconciler.load_phase", "load_phase.py")
_apply_phase = _load("rebar_reconciler.apply_phase", "apply_phase.py")
_persist_phase = _load("rebar_reconciler.persist_phase", "persist_phase.py")

# Retained phase seams owned by the reconcile spine; these are direct entry points
# for tests/guards over the extracted phase functions, not compatibility aliases
# for moved helper/runtime collaborators.
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
        read_local_tickets=_pass_support._read_local_tickets,
        ensure_selection_current=_pass_support.ensure_selection_current,
        narrow_selection_inputs=_pass_support.narrow_selection_inputs,
        no_op_sync_logger_cls=_helpers._NoOpSyncLogger,
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
        commit_binding_store_snapshot=_pass_support._commit_binding_store_snapshot,
        confirm_peer_links=_persist_phase.confirm_peer_links,
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
