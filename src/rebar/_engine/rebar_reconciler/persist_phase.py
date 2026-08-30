#!/usr/bin/env python3
"""persist_phase.py — reconcile_once's persist phase, extracted from reconcile.py.

Ticket piscine-bullish-cowbird (module-size headroom): reconcile.py was at the
locked 800-line cap. ``confirm_peer_links`` (epic a4bd peer-confirmation
evidence), ``save_and_commit_bindings`` (post-apply baseline/peer-link/binding-
store persistence), and ``persist_and_log`` (the final tally + result-dict
assembly) moved here verbatim.

Dependency-injection, not a back-edge: ``save_and_commit_bindings`` and
``persist_and_log`` call collaborators that existing tests monkeypatch THROUGH
``reconcile.<name>`` expecting the patch to alter THIS module's behavior —
``_commit_binding_store_snapshot`` / ``_confirm_peer_links``
(``test_peer_confirmations_backfill.py``, ``test_peer_confirmations_snapshot.py``)
and ``_save_and_commit_bindings`` (``test_live_mode_failure_tally.py``). A plain
function move would silently break those seams (a bare name resolves against its
DEFINING module's globals, not the patched caller's), so reconcile.py keeps thin
wrapper functions that resolve each patchable collaborator as a bare name in
THEIR OWN globals (picking up any monkeypatch) and forward it here as a keyword
argument. ``confirm_peer_links`` itself has no such external patch dependency
(nothing patches its own internals to observe a caller), so it moved as a plain
function and reconcile.py aliases it directly.
"""

from __future__ import annotations

import importlib.util
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

# Same standalone-load bootstrap idiom as reconcile.py: import lazy_load
# normally when package context exists, else load it by file path so this
# module keeps working when exec'd standalone.
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

_helpers = lazy_load("reconcile_helpers", "reconcile_helpers.py")

_advance_baselines = _helpers._advance_baselines
_write_prev_snapshot_key_set = _helpers._write_prev_snapshot_key_set
_build_plan_entries = _helpers._build_plan_entries


def confirm_peer_links(ctx: Any) -> int:
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
    """Post-apply persistence for a writing pass (extracted from ``persist_and_log``).

    Advances the per-binding baselines and peer-link evidence from THIS pass's proven
    snapshot, saves + commits the binding store to the tickets branch, runs the
    comment-id recording invariant, and advances the prev-snapshot key set. Every step is
    fail-open — a store-write hiccup must never break a pass that otherwise succeeded — and
    the whole block runs ONLY under the persistence gate (skipped in no-write mode).

    ``commit_binding_store_snapshot`` and ``confirm_peer_links`` are threaded in as
    keyword arguments by reconcile.py's thin wrapper (rather than imported here) so
    that ``monkeypatch.setattr(reconcile, "_commit_binding_store_snapshot", ...)`` /
    ``"_confirm_peer_links"`` keep taking effect unchanged.
    """
    binding_store = ctx.binding_store
    repo_root = ctx.repo_root
    pass_id = ctx.pass_id
    prev_path = ctx.prev_path

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
        confirm_peer_links(ctx)
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
        if not commit_binding_store_snapshot(binding_store, repo_root, pass_id):
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

    # Ticket 0fa2: outbound-comment recording invariant (posts succeeded, comment_ids
    # gained none), debounced across 2 consecutive passes. Fail-open: never break sync.
    try:
        ctx.invariants_mod.check_comment_id_recording(binding_store, repo_root, pass_id)
    except Exception as exc:  # noqa: BLE001 — invariant check is best-effort; never break sync
        print(f"reconcile: comment-id invariant check failed ({exc})", file=sys.stderr)

    # Advance only Jira-key membership so the next call preserves edge detection
    # without retaining remote field bodies. The full current snapshot remains
    # available as the per-pass diagnostic artifact under bridge_state/snapshots/.
    # In no-write mode prev_path must stay untouched, so this remains inside the
    # persistence gate.
    assert prev_path is not None
    _write_prev_snapshot_key_set(prev_path, ctx.curr_snapshot)


def persist_and_log(
    ctx: Any,
    *,
    save_and_commit_bindings: Callable[[Any], None],
) -> dict:
    """Persist phase: save+commit the binding store, advance the prev snapshot
    (idempotency), tally the truthful applied/failure counts from the manifest,
    close the sync logger, and assemble the result dict.

    ``save_and_commit_bindings`` is threaded in as a keyword argument by
    reconcile.py's thin wrapper (rather than imported here) so that
    ``monkeypatch.setattr(reconcile, "_save_and_commit_bindings", ...)`` keeps
    taking effect unchanged.
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
    from rebar_reconciler.apply_planning import derive_pass_tally

    tally = derive_pass_tally(nowrite_plan, apply_tally, manifest_path, len(mutations))
    mutation_failures = tally["failed"]
    mutations_deferred = tally["deferred"]
    mutations_skipped = tally["skipped"]
    pass_degraded = tally["degraded"]
    # RP-03 S5 T2 (AC5): keep ``recovered`` OUT of applied so the five reported buckets are
    # DISJOINT and sum to ``mutation_count`` (exact tallies). ``derive_pass_tally``'s applied
    # is the legacy folded count (``build_pass_tally`` sets ``applied_count = applied +
    # recovered``; the non-error manifest count includes recovered too), so subtract the
    # explicit ``recovered_count`` back out — legacy/no-write passes carry none → 0.
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
    # RP-03 S3 T3: a degraded cutover pass (any failure OR an opened fuse) must exit
    # non-zero even when its ``failed`` bucket is empty; surface the flag for __main__'s
    # exit gate. Only a cutover apply_tally sets it, so every legacy path stays converged.
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
