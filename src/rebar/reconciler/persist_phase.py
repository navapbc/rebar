#!/usr/bin/env python3
"""Persist reconciliation evidence, binding state, and truthful pass tallies.

``reconcile`` passes patchable collaborators as arguments so tests patching its
phase seams still intercept them after the module split. Peer confirmation
needs no such wrapper.
"""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Callable
from typing import Any

from rebar.reconciler.scratch_retention import prune_bridge_scratch

_helpers = importlib.import_module("rebar.reconciler.reconcile_helpers")

_advance_baselines = _helpers._advance_baselines
_write_prev_snapshot_key_set = _helpers._write_prev_snapshot_key_set
_build_plan_entries = _helpers._build_plan_entries


def confirm_peer_links(ctx: Any) -> int:
    """Backfill then confirm snapshot links on one store instance.

    One instance permits same-pass provenance upgrades and avoids lost updates;
    save once at the end. The caller invokes this only in writing passes.
    """
    from rebar.reconciler.peer_confirmations import (
        backfill_from_managed_refs,
        confirm_from_snapshot,
        open_store,
    )

    store = open_store(ctx.repo_root)
    written = backfill_from_managed_refs(store, ctx.local_tickets, ctx.binding_store)
    written += confirm_from_snapshot(store, ctx.curr_snapshot, ctx.binding_store)
    if written:
        store.save()
    return written


def save_and_commit_bindings(
    ctx: Any,
    *,
    commit_binding_store_snapshot: Callable[..., bool],
    confirm_peer_links: Callable[[Any], int],
) -> None:
    """Persist proven baselines, peer links, and snapshot keys after apply.

    Store and invariant failures are reported without failing the pass. The
    caller passes patchable commit and confirmation seams by keyword.
    """
    binding_store = ctx.binding_store
    repo_root = ctx.repo_root
    pass_id = ctx.pass_id
    prev_path = ctx.prev_path

    # Advance the last-synced ancestor before saving, without breaking the
    # pass if evidence cannot be recorded.
    try:
        _advance_baselines(binding_store, ctx.curr_snapshot, ctx.synced_fields)
    except Exception as exc:  # noqa: BLE001 — baseline advance is best-effort; never break sync
        print(  # noqa: T201
            f"reconcile: baseline advance failed ({exc})",
            file=sys.stderr,
        )
    # Authoritative snapshot links prove peer presence even without a local
    # push. Record that evidence before save; failures remain nonfatal.
    try:
        confirm_peer_links(ctx)
    except Exception as exc:  # noqa: BLE001 — evidence is best-effort; never break sync
        print(  # noqa: T201
            f"reconcile: peer-link confirmation failed ({exc})",
            file=sys.stderr,
        )
    try:
        binding_store.save()
        # Commit bindings to the tickets branch so its next merge cannot
        # discard new bindings and turn updates into creates.
        if not commit_binding_store_snapshot(binding_store, repo_root, pass_id):
            # A failed commit leaves on-disk bindings exposed to the next
            # tickets-branch merge. Report that risk but complete the pass.
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

    # Ticket 0fa2: outbound-comment recording invariant (posts succeeded, comment_ids
    # gained none), debounced across 2 consecutive passes. Fail-open: never break sync.
    try:
        ctx.invariants_mod.check_comment_id_recording(binding_store, repo_root, pass_id)
    except Exception as exc:  # noqa: BLE001 — invariant check is best-effort; never break sync
        print(f"reconcile: comment-id invariant check failed ({exc})", file=sys.stderr)  # noqa: T201

    # Persist only key membership for the next edge diff; retain full fields
    # in the pass diagnostic, and leave no-write snapshots untouched.
    assert prev_path is not None
    _write_prev_snapshot_key_set(prev_path, ctx.curr_snapshot)


def persist_and_log(
    ctx: Any,
    *,
    save_and_commit_bindings: Callable[[Any], None],
) -> dict:
    """Persist when allowed, close logging, and return truthful pass counts.

    The caller supplies its patchable binding-save seam by keyword.
    """
    persist = ctx.persist
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
        save_and_commit_bindings(ctx)

    # Derive applied and failed counts from handler outcomes, not computed
    # mutations. Missing/invalid manifests retain the legacy count fallback;
    # no-write passes report zero applied and use their in-memory plan.
    from rebar.reconciler.apply_planning import derive_pass_tally

    tally = derive_pass_tally(nowrite_plan, apply_tally, manifest_path, len(mutations))
    mutation_failures = tally["failed"]
    mutations_deferred = tally["deferred"]
    mutations_skipped = tally["skipped"]
    pass_degraded = tally["degraded"]
    # Recovered outcomes are folded into the legacy applied tally; subtract
    # them so the reported buckets stay disjoint and sum to mutation_count.
    mutations_recovered = int((apply_tally or {}).get("recovered_count", 0) or 0)
    mutations_applied = max(0, tally["applied"] - mutations_recovered)

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
        mutations_deferred=mutations_deferred,
        mutations_skipped=mutations_skipped,
        mutations_recovered=mutations_recovered,
        recovery_failures=recovery_failures,
    )
    sync_logger.close()
    # Bound the repo-local scratch under .rebar/bridge/ once every artifact of this pass is
    # written. A no-write pass writes no scratch, so it prunes none either.
    if persist:
        prune_bridge_scratch(ctx.repo_root)

    result = {
        "pass_id": pass_id,
        "mutation_count": len(mutations),
        "mutations_applied": mutations_applied,
        "mutation_failures": mutation_failures,
        "mutations_deferred": mutations_deferred,
        "mutations_skipped": mutations_skipped,
        "mutations_recovered": mutations_recovered,
        "recovery_failures": recovery_failures,
        "manifest_path": str(manifest_path) if manifest_path is not None else None,
    }
    # Surface cutover degradation even when its failure bucket is empty.
    if pass_degraded:
        result["degraded"] = True
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
