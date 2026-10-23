#!/usr/bin/env python3
"""Run the invariant, snapshot, outbound, inbound, and binding-walk diff phases.

The phases append typed mutations to ``ctx.mutations``. ``_load`` provides a
patchable sibling-import seam, and loose context typing avoids a back-edge to
``reconcile.py``. The outbound phase lives in ``outbound_pass.py``; alert emitters
stay here for by-path tests that patch this module's loader.
"""

from __future__ import annotations

import importlib.util
import json
import sys
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

# Re-export the outbound phase so callers and tests can patch this module's name.
# Its alert callbacks remain here for by-path tests that patch ``_load``.
from rebar.reconciler.outbound_pass import _run_differs_outbound


def _load(_name: str, relpath: str):
    """Import a sibling named by ``relpath``; retain ``_name`` for callers."""
    return importlib.import_module(
        "rebar.reconciler." + relpath.removesuffix(".py").replace("/", ".")
    )


def _emit_outbound_field_alerts(
    conflict_sink: list[tuple[str, str]],
    dropped_field_sink: list[tuple[str, str]],
    repo_root: Path,
    pass_id: str,
) -> None:
    """Emit 24-hour-deduped conflict and dropped-field alerts without failing the pass."""
    if not conflict_sink and not dropped_field_sink:
        return
    try:
        alert_store = _load("rebar.reconciler.alert_store", "alert_store.py")
    except Exception:  # noqa: BLE001 — observability only; never break the pass
        return
    for kind, sink in (
        ("outbound-field-conflict", conflict_sink),
        ("outbound-field-dropped", dropped_field_sink),
    ):
        for jira_key, field in sink:
            dedup_key = f"{kind}:{jira_key}:{field}"
            try:
                if not alert_store.is_deduped(dedup_key, repo_root=repo_root):
                    alert_store.append(
                        {
                            "kind": kind,
                            "key": dedup_key,
                            "jira_key": jira_key,
                            "field": field,
                            "pass_id": pass_id,
                            "timestamp_ns": time.time_ns(),
                        },
                        repo_root=repo_root,
                    )
            except Exception:  # noqa: BLE001 — best-effort alert write
                pass


def _emit_recovery_failure_alerts(
    failures: list[dict[str, Any]],
    repo_root: Path,
    pass_id: str,
) -> None:
    """Emit 24-hour-deduped recovery-failure alerts without failing the pass."""
    if not failures:
        return
    try:
        alert_store = _load("rebar.reconciler.alert_store", "alert_store.py")
    except Exception:  # noqa: BLE001 — observability only; never break the pass
        return
    for failure in failures:
        local_id = failure.get("local_id", "<unknown>")
        dedup_key = f"outbound-recovery-failure:{local_id}"
        try:
            if not alert_store.is_deduped(dedup_key, repo_root=repo_root):
                alert_store.append(
                    {
                        "kind": "outbound-recovery-failure",
                        "key": dedup_key,
                        "local_id": local_id,
                        "reason": failure.get("reason"),
                        "pass_id": pass_id,
                        "timestamp_ns": time.time_ns(),
                    },
                    repo_root=repo_root,
                )
        except Exception:  # noqa: BLE001 — best-effort alert write
            pass


def _load_reconcile_backend():
    """Load the configured backend for transport and injected field mappers."""
    from rebar.config import compose_config
    from rebar.reconciler._backend_registry import select_backend

    return select_backend(compose_config())


def _read_local_ticket_full(repo_root: Path, local_id: str, *, no_sync: bool) -> dict | None:
    """Read a local ticket, including archived ones; return ``None`` on failure.

    ``no_sync`` suppresses the tickets-branch fetch for a no-write pass.
    """
    import os as _os
    import subprocess as _sp

    from rebar._engine import in_process_cli

    cli = Path(in_process_cli())
    if not cli.exists():
        return None
    # Build an explicit env so the live path (no_sync=False) does NOT inherit an
    # ambient REBAR_SYNC_PULL=off from the caller — that would silently suppress
    # the tickets-branch sync pull even though a syncing read was requested.
    _env = dict(_os.environ)
    if no_sync:
        _env["REBAR_SYNC_PULL"] = "off"
    else:
        _env.pop("REBAR_SYNC_PULL", None)
    try:
        result = _sp.run(
            # ``--include-provenance`` keeps ``managed_refs`` (the monotonic
            # removal-sync projection) in the output — the outbound differ reads it
            # to decide REMOVE-vs-ADOPT. It is stripped from the default human view,
            # so the reconciler MUST always pass this flag here.
            [str(cli), "show", "--include-provenance", local_id],
            capture_output=True,
            text=True,
            cwd=str(repo_root),
            timeout=30,
            env=_env,
            check=False,
        )
        if result.returncode != 0:
            return None
        data = json.loads(result.stdout)
        return data if isinstance(data, dict) else None
    except Exception:  # noqa: BLE001 — fail-open: a failed read → ABSENT (no action)
        return None


def run_differs(ctx: Any) -> None:
    """Run diff phases and collect typed mutations on ``ctx``.

    The snapshot differ receives two Jira snapshots here despite its
    local/remote contract. Drop its local-state-dependent output; retain new
    Jira issue creates. If real local state replaces the first snapshot, revisit
    that suppression. Loose context typing avoids importing ``reconcile.py``.
    """
    # Refresh scoped bound keys before diffing or baseline advance so search lag
    # cannot overwrite a just-synced field.
    from rebar.reconciler.snapshot_lagfree_refresh import refresh_scoped_snapshot

    refresh_scoped_snapshot(ctx)

    skip_invariant_filing, quarantine_keys, seed_repair_property_mutations = (
        _run_differs_invariants(ctx)
    )

    # Compute mutations (pure function, no I/O). The invariant signals are
    # passed through so the differ honors quarantine + seed mutations.
    mutations = ctx.differ.compute_mutations(
        ctx.prev_snapshot,
        ctx.curr_snapshot,
        quarantine_set=quarantine_keys,
        seed_mutations=seed_repair_property_mutations,
    )
    # Bugs 727f / d103: drop the differ arms that only make sense when `local_state` is
    # really local state — see this function's docstring and the helper's.
    from rebar.reconciler.reconcile_helpers import drop_snapshot_differ_local_state_emissions

    mutations = drop_snapshot_differ_local_state_emissions(mutations)
    from rebar.reconciler.reconcile_helpers import drop_snapshot_differ_property_bound_emissions

    mutations = drop_snapshot_differ_property_bound_emissions(mutations, ctx.curr_snapshot)

    _run_differs_report_schema_drift(mutations, skip_invariant_filing, ctx.invariants_mod)
    # Ticket 4af8: obtain the configured backend ONCE and thread its role Protocols
    # into both differ phases (transport for the live-comment fetch; .outbound/.inbound as
    # the injected field mappers) so the core differs name no vendor mapper.
    backend = _load_reconcile_backend()
    outbound_raw, absent_alive_fields, outbound_diff_client = _run_differs_outbound(
        ctx, mutations, backend
    )
    _run_differs_inbound(ctx, mutations, outbound_raw, absent_alive_fields, backend)
    _run_differs_binding_walk(ctx, mutations, outbound_diff_client)

    ctx.mutations = mutations

    # Derive a pure ticket plan from finalized mutations and pass inputs. Keep
    # mutations authoritative; getattr defaults support partial test contexts.
    ticket_planner = _load("rebar.reconciler.ticket_planner", "ticket_planner.py")
    prev_snapshot = getattr(ctx, "prev_snapshot", {}) or {}
    # Use an enum's value; accept plain-string modes in test contexts.
    _target_mode = getattr(ctx, "target_mode", None)
    mode = str(getattr(_target_mode, "value", _target_mode) or "")
    # The planner accepts typed mutations with ``target``; legacy dicts lack it.
    # Duck typing also tolerates by-path module loads.
    typed_mutations = [m for m in ctx.mutations if getattr(m, "target", None) is not None]
    ctx.observation, ctx.ticket_plans = ticket_planner.plan_pass(
        pass_id=getattr(ctx, "pass_id", ""),
        local_snapshot=prev_snapshot,
        remote_snapshot=getattr(ctx, "curr_snapshot", {}) or {},
        binding_view={},
        mode=mode,
        selection={
            "kind": getattr(ctx, "selection_kind", None),
            "ids": ticket_planner.scoped_selection_ids(
                set(getattr(ctx, "selection_ids", None) or []), typed_mutations
            ),
        },
        limits={"max_changes": getattr(ctx, "max_changes", None)},
        mutations=typed_mutations,
    )
    # Additive parity check (RP-03 S2 T3): a PURE comparison of the shadow plans against
    # the same filtered legacy mutations, recorded on ctx for downstream reporting. No
    # I/O — ``compare_parity`` is a pure function of its frozen inputs.
    ctx.plan_parity = ticket_planner.compare_parity(typed_mutations, ctx.ticket_plans)


def _run_differs_invariants(ctx: Any) -> tuple[bool, set[str], list]:
    """Invariant phase: at-most-one-local-id filing + dual-identity round-trip.

    Returns ``(skip_invariant_filing, quarantine_keys, seed_repair_property_mutations)``
    — the ``skip_invariant_filing`` gate reused by later phases to suppress bug-filing
    side effects, plus the quarantine set + seed mutations the differ honors.
    """
    persist = ctx.persist
    # Both legacy filtering and canonical selection scope the pass, so suppress
    # global side effects for either.  Canonical selection has already narrowed
    # differ inputs; it must never flow into the legacy post-differ write filter.
    scoped_ids = ctx.filter_local_ids or ctx.selection_ids
    repo_root = ctx.repo_root
    invariants_mod = ctx.invariants_mod
    prev_snapshot = ctx.prev_snapshot
    curr_snapshot = ctx.curr_snapshot

    # File at most 5 structural violations from the post-fetch snapshot.
    # Skip filing in filtered or no-write passes to avoid out-of-scope writes.
    skip_invariant_filing = (not persist) or bool(scoped_ids)
    if skip_invariant_filing:
        _reason = "no-write mode" if not persist else "filtered pass"
        # Diagnostic line → stderr so no-write mode keeps STDOUT a pure JSON
        # payload (the computed plan) for library/MCP callers.
        print(  # noqa: T201
            f"invariants: skipped ({_reason}, {len(curr_snapshot)} issues in snapshot)",
            file=sys.stderr,
        )
    else:
        filed = invariants_mod.check_at_most_one_local_id(curr_snapshot, repo_root=repo_root)
        print(f"invariants: scanned={len(curr_snapshot)} filed={len(filed)} (cap=5)")  # noqa: T201

    # Quarantine one-sided keys and seed same-pass identity repairs. Filtered
    # passes skip this to avoid mutations outside their scope.
    if skip_invariant_filing:
        quarantine_keys: set[str] = set()
        seed_repair_property_mutations: list = []
    else:
        quarantine_keys, seed_repair_property_mutations = (
            invariants_mod.check_dual_identity_complete(prev_snapshot, curr_snapshot)
        )
    return skip_invariant_filing, quarantine_keys, seed_repair_property_mutations


def _run_differs_report_schema_drift(mutations, skip_invariant_filing, invariants_mod) -> None:
    """Post-emit phase: file dedup'd bug tickets for repair_property schema-drift follow-ons."""
    # File deduplicated bugs and alerts for repair_property follow-ons whose
    # kind is ``schema_drift_signal``. Accept typed mutations and legacy dicts;
    # prefer ``issue_key`` and fall back to ``target``.
    mut_mod_for_action = _load("reconcile_mutation", "mutation.py")
    for _m in mutations:
        action_attr = getattr(_m, "action", None)
        if action_attr is not None:
            # Typed Mutation shape. Normalise enum/string for comparison.
            action_str = getattr(action_attr, "value", action_attr)
            payload = getattr(_m, "payload", None) or {}
            follow_on = payload.get("follow_on") if isinstance(payload, Mapping) else None
        else:
            # Legacy dict shape.
            action_str = _m.get("action")
            follow_on = _m.get("follow_on")
        if action_str != mut_mod_for_action.MutationAction.repair_property.value:
            continue
        if isinstance(follow_on, Mapping) and follow_on.get("kind") == "schema_drift_signal":
            # report_schema_drift FILES a local bug ticket + writes an alert
            # record (CREATE events). Skip in no-write / filtered passes.
            if skip_invariant_filing:
                continue
            invariants_mod.report_schema_drift(
                follow_on.get("issue_key") or follow_on.get("target"),
                follow_on.get("observed"),
                follow_on.get("expected"),
            )


def _run_differs_inbound(ctx: Any, mutations, outbound_raw, absent_alive_fields, backend) -> None:
    """Inbound differ phase (binding-aware): Jira -> local for bound tickets;
    convert each InboundMutation -> typed Mutation onto ``mutations``.

    ``backend`` is the configured :class:`Backend` (ticket 4af8); its ``.inbound`` mapper
    is injected into ``compute_inbound_mutations``.
    """
    local_tickets = ctx.local_tickets
    inbound_differ_mod = ctx.inbound_differ_mod
    binding_store = ctx.binding_store
    sync_logger = ctx.sync_logger
    curr_snapshot = ctx.curr_snapshot
    mut_mod = _load("reconcile_mutation", "mutation.py")

    # -------------------------------------------------------------------
    # Inbound differ (binding-aware): Jira → local for bound tickets.
    #
    # This coexists with the legacy snapshot-diff inbound path above. The
    # legacy path handles unbound Jira issues (create/delete/probe); this
    # path handles field-level updates for already-bound tickets.
    # -------------------------------------------------------------------
    local_by_id = {t.get("ticket_id", t.get("id", "")): t for t in local_tickets}
    # Share outbound mutations to suppress contradictory inbound updates. Add
    # alive absent-issue GET fields to a copy so both differs see them without
    # changing the persisted snapshot.
    inbound_snapshot = curr_snapshot
    if absent_alive_fields:
        inbound_snapshot = dict(curr_snapshot)
        inbound_snapshot.update(absent_alive_fields)
    inbound_new, _ib_suppressed = inbound_differ_mod.compute_inbound_mutations(
        inbound_snapshot,
        binding_store,
        local_by_id,
        outbound_mutations=outbound_raw,
        inbound_mapper=backend.inbound,
        outbound_mapper=backend.outbound,
    )
    sync_logger.log(
        "inbound_differ_complete",
        count=len(inbound_new),
    )
    _ib_with_fields = sum(1 for m in inbound_new if m.fields)
    _ib_with_labels = sum(1 for m in inbound_new if m.labels)
    _ib_with_comments = sum(1 for m in inbound_new if getattr(m, "comments", []))
    print(  # noqa: T201
        f"RECON: inbound_differ total={len(inbound_new)} "
        f"with_fields={_ib_with_fields} with_labels={_ib_with_labels} "
        f"with_comments={_ib_with_comments}",
        file=sys.stderr,
    )
    print(  # noqa: T201
        f"RECON: bidir_suppressed inbound={_ib_suppressed}",
        file=sys.stderr,
    )

    # Convert InboundMutation → typed Mutation for unified dispatch.
    for im in inbound_new:
        typed = mut_mod.Mutation(
            direction=mut_mod.MutationDirection.inbound,
            action=mut_mod.MutationAction.update,
            target=im.jira_key,
            payload={
                "local_id": im.local_id,
                # The inbound applier reads ``fields``; ``changed_fields`` would
                # hide edits and status changes inside the wrapper payload.
                "fields": im.fields,
                "labels": im.labels,
                # Bug 85a1 (Gap 1): propagate inbound comments so each new
                # Jira comment is written as a local COMMENT event with
                # jira_comment_id binding. getattr default keeps backward
                # compat with InboundMutation variants (legacy test stubs)
                # that lack the field.
                "comments": getattr(im, "comments", []),
                # Cycle 3: inbound link adds — _apply_inbound_update writes
                # each into rebar via the rebar.link library facade.
                "links": getattr(im, "links", []),
            },
            provenance={
                "source": "inbound_differ",
                "jira_key": im.jira_key,
                "local_id": im.local_id,
            },
        )
        mutations.append(typed)


def _run_differs_binding_walk(ctx: Any, mutations, outbound_diff_client) -> None:
    """Binding-store-driven acting walk (drift classes A + C); extend ``mutations``."""
    repo_root = ctx.repo_root
    persist = ctx.persist
    local_tickets = ctx.local_tickets
    binding_store = ctx.binding_store
    curr_snapshot = ctx.curr_snapshot
    outbound_differ_mod = ctx.outbound_differ_mod
    sync_logger = ctx.sync_logger
    mut_mod = _load("reconcile_mutation", "mutation.py")

    # Walk bindings to process archived/deleted local tickets absent from the
    # active snapshot. The breaker gates terminal and probe/retirement actions.
    binding_walk_mod = _load("reconcile_binding_walk", "binding_walk.py")
    classify_mod = _load("reconcile_classify", "classify.py")
    from rebar.config import ConfigError, compose_config

    try:
        _max_acting_fraction = compose_config().reconciler.max_acting_fraction
    except ConfigError:
        _max_acting_fraction = 0.10
    _active_local_ids = {t.get("ticket_id") for t in local_tickets if t.get("ticket_id")}
    # Keys the EDGE-triggered differ already emits an inbound-create for this pass
    # (new since prev_snapshot) — the level-triggered adopt must not double-create
    # them; it only heals steady-state misses (present in prev AND curr, unbound).
    _already_created = {
        getattr(m, "target", None)
        for m in mutations
        if getattr(m, "direction", None) == mut_mod.MutationDirection.inbound
        and getattr(m, "action", None) == mut_mod.MutationAction.create
        and getattr(m, "target", None)
    }
    walk = binding_walk_mod.compute_binding_walk_mutations(
        binding_store,
        curr_snapshot,
        _active_local_ids,
        client=outbound_diff_client,
        local_reader=lambda lid: _read_local_ticket_full(repo_root, lid, no_sync=not persist),
        max_acting_fraction=_max_acting_fraction,
        classify_mod=classify_mod,
        mutation_mod=mut_mod,
        outbound_differ_mod=outbound_differ_mod,
        persist=persist,
        skip_adopt_keys=_already_created,
    )
    sync_logger.log(
        "binding_walk_complete",
        acting=len(walk.mutations),
        retired=len(walk.retired),
        probed=len(walk.probed),
        alerted=len(walk.alerted),
        adopted=len(walk.adopted),
        breaker_allowed=bool(walk.breaker.allowed) if walk.breaker else True,
        refused=walk.refused,
    )
    # Emit the classify census under its canonical ``decision_census`` name.
    sync_logger.log("decision_census", **walk.census)
    if walk.refused:
        sync_logger.log(
            "binding_walk_breaker_refused",
            reason=walk.breaker.reason if walk.breaker else "",
        )
    for _alert_key in walk.alerted:
        sync_logger.log("binding_walk_alert", jira_key=_alert_key)
    mutations.extend(walk.mutations)
