#!/usr/bin/env python3
"""Inbound leaf appliers: materialise Jira issues into the local ticket store.

The (inbound, *) leaf handlers dispatched from the typed registry, plus
inbound_repair_property (the local_id property write _apply_inbound_repair_property
delegates to). Each leaf reads its Jira-side payload, translates it via
inbound_translate, and writes local ticket events; rebar-id/property write-backs
to Jira run through batch_dispatch._call_with_retry.

Imports downward only (apply_base, batch_dispatch, inbound_translate — none import
back); never imports applier.

Bug 3b5f removed the ``delete`` and ``probe`` inbound leaves: their producer chain had
no live emitter, and ``_apply_inbound_delete``'s ``create_after_hard_delete`` follow-on
WAS the resurrection of a deliberately-deleted Jira issue that the operator ruling
forbids. A confirmed hard-delete now tombstones the pairing in ``outbound_differ``
instead — see ADR 0028's Consequences.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ._backend import TicketTransport

from rebar_reconciler.apply_base import (
    ApplyResult,
    _direction_guard,
    _load_mutation_module,
)
from rebar_reconciler.apply_inbound_events import (
    # Loop-breaker marker re-exported so ``apply_inbound._RECONCILER_MARKER_APPLIER``
    # still resolves (its sole use — the create comment-bootstrap — moved with the
    # phase helpers, ticket 090a, and moved again with them to apply_inbound_events.py
    # when apply_inbound_records.py was split, ticket 6f51-f8a4-b4fb-450c).
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
from rebar_reconciler.apply_inbound_records import _inbound_update_apply_links
from rebar_reconciler.batch_dispatch import _call_with_retry
from rebar_reconciler.inbound_translate import (
    _jira_key_to_local_id,
    _resolve_tracker_dir,
)
from rebar_reconciler.pass_io import _write_mapping_atomic

logger = logging.getLogger(__name__)


def _apply_inbound_create(
    mutation, *, client: TicketTransport | None = None, repo_root=None, binding_store=None
) -> ApplyResult:
    """Materialise a remote Jira issue as a local jira-* ticket.

    Writes a CREATE event (title, ticket_type, priority, description, tags
    including ``imported:reconciler-bootstrap``) and, when the payload carries
    a non-default status, a follow-up STATUS event reverse-mapped via
    config.local_to_jira_status.
    """
    mut_mod = _load_mutation_module()
    _direction_guard(mutation, mut_mod.MutationDirection.inbound)

    payload = dict(mutation.payload or {})
    # Accept both shapes: payload with nested "fields" key (batch-dict shape)
    # and payload with top-level field keys (differ Mutation shape).
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

    # Defense-in-depth inbound dedup (ticket 1577). The snapshot differ normally
    # stands down for an already-bound issue by recognising its rebar-id:<local_id>
    # label in the fetched fields (bug 4354) — that is the primary production
    # dedup. This guard covers the narrow transient where the snapshot predates
    # the label write-back yet the binding already exists in bindings.json: the
    # differ then mis-emits an inbound CREATE which would materialise a duplicate
    # local ticket. If the target Jira key is already bound, record the mapping
    # and skip materialisation. Cheap (local reverse-index lookup, no Jira GET).
    if binding_store is not None:
        bound_local_id = binding_store.get_local_id(jira_key)
        if bound_local_id:
            if repo_root is None:
                from rebar.config import reconciler_repo_root as _owned_repo_root

                repo_root = _owned_repo_root()
            mapping_path = repo_root / "bridge_state" / "mapping.json"
            _write_mapping_atomic(mapping_path, bound_local_id, jira_key)
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
    _inbound_create_record_binding(mutation, binding_store, local_id, jira_key)
    _inbound_create_writeback_jira(client, jira_key, local_id, tracker_dir)

    return ApplyResult(
        mutation.direction,
        mutation.action,
        {"local_id": local_id, "create_event": str(create_path)},
    )


def _apply_inbound_update(mutation, *, repo_root=None, **_kwargs) -> ApplyResult:
    """Apply a remote-side update to an existing local jira-* ticket.

    Writes one EDIT event with the changed fields, plus an additional STATUS
    event when the payload includes a Jira status change. Unknown ticket
    directories are tolerated (the EDIT is still written; the reducer will
    surface fsck on the next read).
    """
    mut_mod = _load_mutation_module()
    _direction_guard(mutation, mut_mod.MutationDirection.inbound)

    payload = dict(mutation.payload or {})
    # Accept both shapes: payload with nested "fields" key (batch-dict shape)
    # and payload with top-level field keys (differ Mutation shape).
    fields = payload.get("fields") or payload
    target = mutation.target
    # Bug 1bb2: prefer payload['local_id'] (set by reconcile.py for bound
    # tickets) over Jira-key-derived local_id. Without this, EDIT events
    # for a bound UUID ticket are written under a new jira-dig-NNN/
    # directory — creating a duplicate ticket and silently dropping the
    # update on the bound UUID ticket.
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
    misshapen payload). All client calls go through :func:`_call_with_retry`
    so transient 5xx/429/timeout failures retry with backoff.
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
        _call_with_retry(client.remove_label, mutation.target, label)
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
    """Emit a ``suppress_pair`` follow-on and a ``pending_bug_ticket`` directive
    for an unresolved (local, Jira) conflict.

    Bug filing is DEFERRED out of the apply loop because the
    ``rebar create bug`` CLI commits to the ``tickets``
    orphan branch, which would advance HEAD inside ``_apply_batch``'s
    drift-guarded loop and raise a spurious ``HeadDriftError`` (bug d822).
    The caller (``apply``) collects pending_bug_ticket directives during the
    inbound dispatch loop and files them after ``_apply_batch`` returns —
    outside the drift guard's scope.

    The follow_on still emits during the leaf so reconcile_once can drop
    subsequent mutations for the same pair in the same pass; suppression
    semantics are unchanged.
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
    """Repair a missing entity property on a Jira issue.

    Happy path: invokes ``client.set_issue_property(target, 'local_id', local_id)``
    and returns ``{'status': 'ok', 'key': target}``.

    Failure path: when ``set_issue_property`` raises, attempts a follow-on cleanup
    via ``client.remove_label(target, 'rebar-id-<local_id>')`` (best-effort — a
    ``remove_label`` exception is captured, NOT raised), and returns an outcome
    dict with ``status='repair_property_failed'`` plus a top-level ``follow_on``
    payload whose ``kind`` is ``'schema_drift_signal'``. The follow-on is the
    signalling seam consumed by reconcile.py; this function MUST NOT import
    invariants directly — preserving the invariants-as-upstream-phase contract
    (see ticket 44e6-4916 AC: "applier.py does NOT import invariants").

    The follow_on field sits at the TOP LEVEL of the outcome dict (not nested
    under 'result'); manifest canonical-form serialization is expected to
    EXCLUDE follow_on fields when computing the content-addressable hash
    (per AC amendment G2 on ticket 44e6-4916).

    Args:
        mutation: Object exposing ``.target`` (Jira issue key) and ``.payload``
                  (mapping with at least a ``'local_id'`` entry).
        client:   AcliClient (or compatible test double) exposing
                  ``set_issue_property`` and ``remove_label``.

    Returns:
        Outcome dict — see status taxonomy above.
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
