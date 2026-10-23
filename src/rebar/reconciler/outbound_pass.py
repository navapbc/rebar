#!/usr/bin/env python3
"""Outbound differ sub-pass: recover bindings, plan changes, and type mutations.

``run_differs`` retains the alert emitters because by-path-loaded tests patch
that module's loader; local imports here preserve the patch seam. Sibling
loading remains patchable, and ``ctx`` avoids a reverse import of pass context.
"""

from __future__ import annotations

import importlib.util
import sys
from typing import Any

# ADR 0107 "Cut" step: producers construct native typed payload dataclasses
# directly instead of raw dicts. mutation_payloads.py carries no cross-reload
# class-identity requirement (it never references MutationDirection/
# MutationAction), so an ordinary package import is safe here.
from rebar.reconciler.mutation_payloads import (
    OutboundCreatePayload,
    OutboundDeletePayload,
    OutboundUpdatePayload,
)


def _load(_name: str, relpath: str):
    """Import a sibling through the patchable package loader seam."""
    return importlib.import_module(
        "rebar.reconciler." + relpath.removesuffix(".py").replace("/", ".")
    )


def _run_differs_outbound(ctx: Any, mutations, backend) -> tuple[list, dict, Any]:
    """Append typed outbound mutations and return raw plans, alive fields, and client.

    The configured backend supplies transport, outbound mapping, and link
    handling for subsequent inbound and binding-walk phases.
    """
    # Recovery is a whole-store side effect, so any scoped pass suppresses it.
    # This is separate from the legacy post-differ write filter in reconcile.py.
    scoped_ids = ctx.filter_local_ids or ctx.selection_ids
    binding_store = ctx.binding_store
    local_tickets = ctx.local_tickets
    local_label_intent_mod = ctx.local_label_intent_mod
    tracker_dir = ctx.tracker_dir
    repo_root = ctx.repo_root
    outbound_differ_mod = ctx.outbound_differ_mod
    pass_id = ctx.pass_id
    prev_snapshot = ctx.prev_snapshot
    curr_snapshot = ctx.curr_snapshot
    sync_logger = ctx.sync_logger
    mut_mod = _load("reconcile_mutation", "mutation.py")

    # Recovery needs the configured backend transport before the differ uses
    # that same client for live comments; typed mutations then share applier dispatch.
    outbound_diff_client = backend.transport

    # Scoped and no-write passes skip recovery: it can finalize bindings and
    # issue remote label/property writes outside their permitted boundary.
    ctx.recovery_failures = 0
    if not scoped_ids and ctx.persist:
        recovery_failures: list[dict[str, Any]] = []
        try:
            binding_store.recover_pending_bindings(
                outbound_diff_client, failure_sink=recovery_failures
            )
        except Exception as exc:  # noqa: BLE001 — fail-open: a total recovery failure is non-fatal
            recovery_failures.append({"local_id": "<all>", "reason": repr(exc)})
            print(  # noqa: T201
                f"reconcile: binding recovery failed ({exc}), continuing",
                file=sys.stderr,
            )
        # LOUD (story 9622): surface per-entry failures instead of the silent
        # swallow — a deduped bridge alert each + a nonzero recovery_failures tally.
        if recovery_failures:
            ctx.recovery_failures = len(recovery_failures)
            # Local import: ``_emit_recovery_failure_alerts`` stayed in run_differs.py
            # (see module docstring) — resolve it against THAT module's namespace so
            # a monkeypatch/patch.object on run_differs still intercepts it.
            from rebar.reconciler.run_differs import _emit_recovery_failure_alerts

            _emit_recovery_failure_alerts(recovery_failures, repo_root, pass_id)

    # Compute intent before diffing bound tickets, so peer-only labels do not
    # become outbound removals that cancel legitimate inbound additions.
    bound_local_ids = [
        t.get("ticket_id", t.get("id", ""))
        for t in local_tickets
        if binding_store.get_jira_key(t.get("ticket_id", t.get("id", ""))) is not None
    ]
    local_label_intent = local_label_intent_mod.compute_label_intent_map(
        bound_local_ids, tracker_dir
    )

    # Share alive absent-key GET fields with inbound diff; omit 404/transport
    # failures. Collect conflict and excluded-field drops for deduped alerts.
    conflict_sink: list[tuple[str, str]] = []
    dropped_field_sink: list[tuple[str, str]] = []
    # Story d19d: hand the differ the store's projects mapping so the create path
    # can resolve each ticket's target project. An ABSENT projects.json loads as an
    # empty Mapping (legacy single-project behaviour); a MALFORMED one fails closed.
    from rebar.reconciler import projects_store as _projects_store_mod

    _projects_mapping = _projects_store_mod.load_mapping(repo_root)
    outbound_raw, absent_alive_fields = outbound_differ_mod.compute_outbound_mutations(
        local_tickets,
        curr_snapshot,
        binding_store,
        outbound_differ_mod.OutboundDiffConfig(
            excluded_statuses={"archived", "deleted"},
            local_label_intent=local_label_intent,
            client=outbound_diff_client,
            pass_id=pass_id,
            prev_snapshot=prev_snapshot,
            conflict_sink=conflict_sink,
            dropped_field_sink=dropped_field_sink,
            projects_mapping=_projects_mapping,
            repo_root=repo_root,
        ),
        outbound_mapper=backend.outbound,
        inbound_mapper=backend.inbound,
        links=backend,
    )
    # Local import: ``_emit_outbound_field_alerts`` stayed in run_differs.py (see
    # module docstring) — resolve it against THAT module's namespace so a
    # monkeypatch/patch.object on run_differs still intercepts it.
    from rebar.reconciler.run_differs import _emit_outbound_field_alerts

    _emit_outbound_field_alerts(conflict_sink, dropped_field_sink, repo_root, pass_id)
    sync_logger.log(
        "outbound_differ_complete",
        count=len(outbound_raw),
    )
    # Stable RECON prefix gives probes per-action counts without parsing JSON.
    _ob_creates = sum(1 for m in outbound_raw if m.action == "create")
    _ob_updates = sum(1 for m in outbound_raw if m.action == "update")
    _ob_deletes = sum(1 for m in outbound_raw if m.action == "delete")
    print(  # noqa: T201
        f"RECON: outbound_differ total={len(outbound_raw)} "
        f"create={_ob_creates} update={_ob_updates} delete={_ob_deletes}",
        file=sys.stderr,
    )

    # Construct typed payloads here so batch dispatch need not infer their shape.
    for om in outbound_raw:
        if om.action == "create":
            typed = mut_mod.Mutation(
                direction=mut_mod.MutationDirection.outbound,
                action=mut_mod.MutationAction.create,
                target=om.local_id,
                payload=OutboundCreatePayload(
                    fields=om.fields,
                    comments=tuple(om.comments),
                    labels=tuple(om.labels),
                    requires_create=tuple(getattr(om, "requires_create", ())),
                    local_id=om.local_id,
                ),
                provenance={"source": "outbound_differ", "local_id": om.local_id},
            )
        elif om.action == "update":
            typed = mut_mod.Mutation(
                direction=mut_mod.MutationDirection.outbound,
                action=mut_mod.MutationAction.update,
                target=om.jira_key or om.local_id,
                payload=OutboundUpdatePayload(
                    changed_fields=om.fields,
                    comments=tuple(om.comments),
                    labels=tuple(om.labels),
                    # Cycle 3: link adds ride the existing update payload
                    # (no new MutationAction) — _apply_outbound_update reads
                    # payload["links"] and calls client.set_relationship.
                    links=tuple(getattr(om, "links", [])),
                ),
                provenance={
                    "source": "outbound_differ",
                    "local_id": om.local_id,
                    "jira_key": om.jira_key,
                },
            )
        elif om.action == "delete":
            typed = mut_mod.Mutation(
                direction=mut_mod.MutationDirection.outbound,
                action=mut_mod.MutationAction.delete,
                target=om.jira_key or om.local_id,
                payload=OutboundDeletePayload(),
                provenance={
                    "source": "outbound_differ",
                    "local_id": om.local_id,
                    "jira_key": om.jira_key,
                },
            )
        else:
            continue  # unknown action — skip
        mutations.append(typed)
    return outbound_raw, absent_alive_fields, outbound_diff_client
