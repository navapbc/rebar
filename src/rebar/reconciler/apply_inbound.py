#!/usr/bin/env python3
"""Apply inbound Jira mutations to local ticket events.

Typed leaves handle create, update, conflict, and property repair. Jira marker
writes are single transport calls. Imports flow downward without an applier
back-edge. Confirmed remote deletion is handled by outbound tombstoning.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ._backend import TicketTransport

from rebar.reconciler.apply_base import (
    ApplyResult,
    _direction_guard,
    _load_mutation_module,
)
from rebar.reconciler.apply_inbound_events import (
    # Preserve the marker's historic import path for callers.
    _RECONCILER_MARKER_APPLIER,  # noqa: F401
    _inbound_create_record_binding,
    _inbound_create_write_create_event,
    _inbound_create_write_status_event,
    _inbound_create_writeback_jira,
    _inbound_update_apply_comments,
    _inbound_update_apply_labels,
    _inbound_update_write_edit_event,
    _inbound_update_write_status_event,
)

# The inbound link graph stays in apply_inbound_records.py — it mutates rebar's shared
# link store through the rebar facade rather than writing event files (ticket
# 6f51-f8a4-b4fb-450c).
from rebar.reconciler.apply_inbound_records import _inbound_update_apply_links
from rebar.reconciler.inbound_translate import (
    _jira_key_to_local_id,
    _resolve_tracker_dir,
)
from rebar.reconciler.pass_io import _write_mapping_atomic
from rebar.reconciler.scratch_retention import mapping_path as _scratch_mapping_path

logger = logging.getLogger(__name__)


def _apply_inbound_create(
    mutation, *, client: TicketTransport | None = None, repo_root=None, binding_store=None
) -> ApplyResult:
    """Create a local ticket from Jira fields, then write non-default status."""
    mut_mod = _load_mutation_module()
    _direction_guard(mutation, mut_mod.MutationDirection.inbound)

    payload = dict(mutation.payload or {})
    # Accept nested batch fields or top-level differ fields.
    fields = payload.get("fields") or payload
    jira_key = mutation.target
    local_id = _jira_key_to_local_id(jira_key)

    # ADOPT gate #1 (epic 3006-e198 / ADR 0027 §4a): never resurrect a RETIRED key
    # (GC'd by class C) into a delete/re-adopt loop — a reappearing key is owned by
    # binding recovery, not a fresh create.
    if binding_store is not None and getattr(binding_store, "is_retired", None):
        if binding_store.is_retired(jira_key):
            detail = {"jira_key": jira_key, "skipped_retired": True}
            return ApplyResult(mutation.direction, mutation.action, detail)

    # If a binding exists before its label appears in the snapshot, record the
    # mapping and skip a duplicate local CREATE.
    if binding_store is not None:
        bound_local_id = binding_store.get_local_id(jira_key)
        if bound_local_id:
            if repo_root is None:
                from rebar.config import reconciler_repo_root as _owned_repo_root

                repo_root = _owned_repo_root()
            _write_mapping_atomic(_scratch_mapping_path(repo_root), bound_local_id, jira_key)
            return ApplyResult(
                mutation.direction,
                mutation.action,
                {
                    "local_id": bound_local_id,
                    "jira_key": jira_key,
                    "dedup_skipped": True,
                },
            )

    tracker_dir, raw_labels, create_path = _inbound_create_write_create_event(
        fields, payload, jira_key, local_id, repo_root
    )
    _inbound_create_write_status_event(fields, raw_labels, tracker_dir, local_id)
    # Coordinated confirmation gate (S4 T3): the local<->jira binding is confirmed
    # (bind_confirm) BEFORE any dependent link/child is released, exactly as the
    # coordinated create composition gates dependents on a confirmed containment
    # (AC5). There is no remote create to double-send on the inbound side, so the
    # materialization sequence is route-invariant. The retired/dedup short-circuits
    # above run FIRST and produce zero early effects.
    _inbound_create_record_binding(mutation, binding_store, local_id, jira_key)
    _inbound_create_writeback_jira(client, jira_key, local_id, tracker_dir)

    return ApplyResult(
        mutation.direction,
        mutation.action,
        {"local_id": local_id, "create_event": str(create_path)},
    )


def _apply_inbound_update(mutation, *, repo_root=None, **_kwargs) -> ApplyResult:
    """Write local EDIT and optional STATUS events for a Jira update.

    Unknown ticket directories are tolerated; the reducer checks them later.
    """
    mut_mod = _load_mutation_module()
    _direction_guard(mutation, mut_mod.MutationDirection.inbound)

    payload = dict(mutation.payload or {})
    # Accept both shapes: payload with nested "fields" key (batch-dict shape)
    # and payload with top-level field keys (differ Mutation shape).
    fields = payload.get("fields") or payload
    target = mutation.target
    # Prefer the bound local ID over a Jira-derived ID to avoid a duplicate.
    local_id = payload.get("local_id") or (
        target if target.startswith("jira-") else _jira_key_to_local_id(target)
    )
    tracker_dir = _resolve_tracker_dir(repo_root)

    written: list[str] = []
    _inbound_update_write_edit_event(fields, tracker_dir, local_id, written, repo_root)
    _inbound_update_write_status_event(fields, tracker_dir, local_id, written)
    _inbound_update_apply_labels(mutation, payload, tracker_dir, local_id, written)
    _inbound_update_apply_comments(payload, tracker_dir, local_id, written)
    links_applied = _inbound_update_apply_links(payload, local_id, repo_root)

    return ApplyResult(
        mutation.direction,
        mutation.action,
        {"local_id": local_id, "events": written, "links_applied": links_applied},
    )


def _apply_inbound_clean_label(
    mutation, *, client: TicketTransport | None = None, **_kwargs
) -> ApplyResult:
    """Remove rebar-id-* labels from a Jira issue.

    Inbound-only leaf: invoked when the differ has detected stale or duplicated
    `rebar-id-*` labels on the Jira side that need to be removed. The mutation
    payload carries the labels to remove under ``labels_to_remove``; only labels
    that match the ``rebar-id-*`` pattern are removed (defensive filter against a
    misshapen payload). Each removal is one transport call: the leaf does not
    retry, so a transient failure propagates and the next pass re-diffs.
    """
    mut_mod = _load_mutation_module()
    _direction_guard(mutation, mut_mod.MutationDirection.inbound)
    if client is None:
        # Stub path: preserved for tests that don't exercise the I/O leaf.
        return ApplyResult(mutation.direction, mutation.action, {})
    labels = mutation.payload.get("labels_to_remove") or []
    removed: list[str] = []
    for label in labels:
        # Defensive: only remove labels matching the rebar-id-* pattern.
        if not isinstance(label, str) or not label.startswith("rebar-id-"):
            continue
        client.remove_label(mutation.target, label)
        removed.append(label)
    return ApplyResult(mutation.direction, mutation.action, {"removed": removed})


def _apply_inbound_repair_property(
    mutation, *, client: TicketTransport | None = None, **_kwargs
) -> ApplyResult:
    """Repair a missing ``local_id`` entity property on a Jira issue.

    Delegates to the existing :func:`inbound_repair_property` implementation
    (kept under its legacy name for back-compat with existing tests). Wraps
    the outcome dict into an ``ApplyResult`` so it routes cleanly through the
    typed-mutation dispatch table.
    """
    mut_mod = _load_mutation_module()
    _direction_guard(mutation, mut_mod.MutationDirection.inbound)
    if client is None:
        # Stub path: preserved for tests that don't exercise the I/O leaf.
        return ApplyResult(mutation.direction, mutation.action, {})
    outcome = inbound_repair_property(mutation, client)
    return ApplyResult(mutation.direction, mutation.action, outcome)


def _apply_inbound_conflict(mutation, **_kwargs) -> ApplyResult:
    """Return pair suppression and a deferred conflict bug directive.

    The caller files the bug after the HEAD-guarded batch to avoid drift;
    same-pass suppression still applies immediately.
    """
    mut_mod = _load_mutation_module()
    _direction_guard(mutation, mut_mod.MutationDirection.inbound)

    payload = dict(mutation.payload or {})
    jira_key = mutation.target
    local_id = payload.get("local_id", "")
    reason = payload.get("reason", "unspecified")
    parent_env = os.environ.get("REBAR_RECONCILER_CONFLICT_PARENT_ID", "")  # read-via: override
    parent_id = payload.get("parent_id") or parent_env
    title = f"[Reconciler conflict]: pair ({local_id!r}, {jira_key!r}) -> {reason}"
    description = (
        f"Reconciler detected a conflict on (local_id={local_id!r}, "
        f"jira_key={jira_key!r}).\n\n"
        f"Reason: {reason}\n\n"
        "## Expected Behavior\n"
        "Conflict is resolved or suppressed before the next reconciler pass.\n\n"
        "## Actual Behavior\n"
        f"Conflict surfaced during applier dispatch with reason={reason!r}."
    )

    follow_on = {
        "kind": "suppress_pair",
        "local_id": local_id,
        "jira_key": jira_key,
    }
    pending_bug_ticket = {
        "title": title,
        "description": description,
        "parent_id": parent_id,
        "local_id": local_id,
        "jira_key": jira_key,
    }
    return ApplyResult(
        mutation.direction,
        mutation.action,
        {
            "follow_on": follow_on,
            "pending_bug_ticket": pending_bug_ticket,
        },
    )


def inbound_repair_property(mutation, client: TicketTransport) -> dict:
    """Write the missing Jira ``local_id`` property and return its outcome.

    On failure, attempt label cleanup and return ``repair_property_failed``
    with a top-level ``schema_drift_signal`` follow-on. Cleanup errors are
    recorded in-band. The manifest excludes follow-ons from its content hash.
    """
    target = mutation.target
    payload = mutation.payload or {}
    local_id = payload.get("local_id", "")

    try:
        client.set_issue_property(target, "local_id", local_id)
        return {"status": "ok", "key": target, "follow_on": None}
    except Exception as exc:  # noqa: BLE001 — local_id write failure: attempt label cleanup, record any cleanup error in-band, return a structured error result
        label_remove_err: Exception | None = None
        try:
            client.remove_label(target, f"rebar-id-{local_id}")
        except Exception as e:  # noqa: BLE001 — best-effort rebar-id label removal during cleanup; its error is captured in-band (label_remove_err)
            label_remove_err = e

        return {
            "status": "repair_property_failed",
            "key": target,
            "follow_on": {
                "kind": "schema_drift_signal",
                "issue_key": target,
                "reason": f"repair_property_failed: {exc}",
                "label_remove_error": (
                    str(label_remove_err) if label_remove_err is not None else None
                ),
            },
        }
