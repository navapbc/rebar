#!/usr/bin/env python3
"""reconcile.py — one-pass orchestrator: fetch → diff → apply.

reconcile_once(pass_id) wires the three reconciler stages into a single
idempotent pass.  Two consecutive calls with an unchanged remote produce
mutation_count=0 on both passes (second call sees prev==curr snapshot).
"""

from __future__ import annotations

import importlib.util
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def _load(_name: str, relpath: str):
    """Import a sibling through the patchable package loader seam."""
    return importlib.import_module(
        "rebar.reconciler." + relpath.removesuffix(".py").replace("/", ".")
    )


def _tracker_dir(repo_root: Path) -> Path:
    """The RELOCATABLE store, RESOLVED: env > ``tracker.dir`` > default under the root."""
    from rebar.config import tracker_dir

    return tracker_dir(repo_root)


# ---------------------------------------------------------------------------
# Load canonical helper owners once. Moved collaborators resolve there at call
# time; this spine retains only its own phase seams (ADR 0111).
# ---------------------------------------------------------------------------
_helpers = _load("rebar.reconciler.reconcile_helpers", "reconcile_helpers.py")
_pass_support = _load("rebar.reconciler.pass_support", "pass_support.py")
_runtime = _load("rebar.reconciler.runtime", "runtime.py")


@dataclass
class _PassContext:
    """Mutable pass state shared by independently callable lifecycle phases."""

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
    # Apply receives the captured backend and transport, avoiding ambient config
    # resolution. A no-write pass without scope may skip composition.
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
    """Fetch, diff both directions, apply, and persist one reconciler pass.

    The previous snapshot under ``<tracker>/.bridge_state`` advances after
    apply, making an unchanged remote idempotent. Scratch lives under
    ``<repo_root>/.rebar/bridge``. ``filter_local_ids`` limits dispatch only;
    the differs still see full inputs. Return pass ID, dispatched mutation
    count, and manifest path.
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
# Load, apply, and persist implementations live in sibling modules. This spine
# retains the lifecycle facade and phase entry points used by guard tests;
# collaborators resolve through their canonical owners.
# ---------------------------------------------------------------------------
_load_phase = _load("rebar.reconciler.load_phase", "load_phase.py")
_apply_phase = _load("rebar.reconciler.apply_phase", "apply_phase.py")
_persist_phase = _load("rebar.reconciler.persist_phase", "persist_phase.py")

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
    """Report corrupt previous state and abort rather than reconcile from it."""
    return _load_phase.handle_corrupt_snapshot(pass_id, repo_root, prev_path, _exc)


def _save_and_commit_bindings(ctx: _PassContext) -> None:
    """Persist proven baselines, peer links, and snapshots for a writing pass.

    The store commit and comment-ID invariant remain fail-open; no-write
    passes skip this phase.
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
