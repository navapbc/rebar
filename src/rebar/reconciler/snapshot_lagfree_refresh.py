#!/usr/bin/env python3
"""Refresh clobber-risk snapshot fields from the primary store.

Search indexing can lag a recent outbound write. The level-triggered inbound
differ could then mirror the stale value over local state even though the
baseline already advanced. Before diffing, direct GETs refresh scoped bound
keys or ambiguous unscoped candidates. Only mirrored scalars are overlaid, so
parent, comment, and link enrichment survives. Failed GETs and 404s leave the
entry untouched for a later pass.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ._backend import TicketTransport

# Only these raw mirrored fields replace search values; enrichment stays intact.
_LAGFREE_SCALAR_FIELDS: tuple[str, ...] = (
    "summary",
    "description",
    "priority",
    "status",
    "assignee",
)
_CANONICAL_MIRRORED_FIELDS: tuple[str, ...] = (
    "title",
    "description",
    "priority",
    "status",
    "assignee",
)


def overlay_lagfree_scalars(
    curr_snapshot: dict[str, dict[str, Any]],
    jira_keys: Iterable[str],
    client: TicketTransport,
) -> int:
    """Overlay direct-GET scalars for present keys; return refreshed entry count.

    Classified 404 or transport failures leave the snapshot and enrichment
    untouched for deferral.
    """
    # Lazy sibling import (the package's by-path load convention keeps module load order
    # free of a hard edge; _safe_get_issue owns the HTTPError/URLError classification).
    from rebar.reconciler.outbound_differ import _safe_get_issue

    refreshed = 0
    for jira_key in jira_keys:
        entry = curr_snapshot.get(jira_key)
        if entry is None:
            continue  # not in this pass's working set — never GET a key we do not arbitrate
        fresh_fields = _safe_get_issue(client, jira_key)
        if not isinstance(fresh_fields, dict):
            # _TRANSPORT_ERROR / _DELETED sentinel — leave the (stale) entry as-is, defer.
            continue
        for field in _LAGFREE_SCALAR_FIELDS:
            if field in fresh_fields:
                entry[field] = fresh_fields[field]
        refreshed += 1
    return refreshed


def refresh_scoped_snapshot(ctx: Any) -> None:
    """Refresh scoped keys or ambiguous unscoped keys before either differ runs.

    A partial context without transport is a no-op. The loose context type
    avoids importing ``reconcile`` back into this module.
    """
    scoped_ids = getattr(ctx, "selection_ids", None) or getattr(ctx, "filter_local_ids", None)
    client = getattr(ctx, "runtime_transport", None)
    if client is None:
        return
    binding_store = getattr(ctx, "binding_store", None)
    curr_snapshot = getattr(ctx, "curr_snapshot", None)
    if binding_store is None or not curr_snapshot:
        return
    jira_keys = []
    if scoped_ids:
        jira_keys = _scoped_jira_keys(scoped_ids, binding_store, curr_snapshot)
    else:
        jira_keys = _unscoped_ambiguous_jira_keys(ctx, binding_store, curr_snapshot)
    if jira_keys:
        overlay_lagfree_scalars(curr_snapshot, jira_keys, client)


def _scoped_jira_keys(
    scoped_ids: Iterable[str], binding_store: Any, curr_snapshot: Mapping[str, Any]
) -> list[str]:
    jira_keys = []
    for local_id in scoped_ids:
        jira_key = binding_store.get_jira_key(local_id)
        if jira_key and jira_key in curr_snapshot:
            jira_keys.append(jira_key)
    return jira_keys


def _unscoped_ambiguous_jira_keys(
    ctx: Any, binding_store: Any, curr_snapshot: Mapping[str, Any]
) -> list[str]:
    get_baseline = getattr(binding_store, "get_baseline", None)
    inbound_mapper = _inbound_mapper_for(ctx)
    if get_baseline is None or inbound_mapper is None:
        return []
    candidates = []
    for local_ticket in getattr(ctx, "local_tickets", None) or []:
        if not isinstance(local_ticket, dict):
            continue
        local_id = local_ticket.get("ticket_id") or local_ticket.get("id")
        if not local_id or _is_pending_binding(binding_store, local_id):
            continue
        jira_key = binding_store.get_jira_key(local_id)
        raw_baseline = get_baseline(local_id)
        if not jira_key or jira_key not in curr_snapshot or not local_ticket or not raw_baseline:
            continue
        if not _raw_remote_differs_from_baseline(curr_snapshot[jira_key], raw_baseline):
            continue
        if _is_ambiguous_snapshot_candidate(
            local_ticket,
            curr_snapshot[jira_key],
            raw_baseline,
            inbound_mapper,
        ):
            candidates.append(jira_key)
    return candidates


def _inbound_mapper_for(ctx: Any) -> Any:
    backend = getattr(ctx, "runtime_backend", None)
    inbound_mapper = getattr(backend, "inbound", None)
    if inbound_mapper is not None:
        return inbound_mapper
    try:
        from rebar.config import compose_config
        from rebar.reconciler._backend_registry import select_backend

        return select_backend(compose_config()).inbound
    except Exception:  # noqa: BLE001 — partial test ctx: no mapper means safe no-op
        return None


def _is_pending_binding(binding_store: Any, local_id: str) -> bool:
    is_pending = getattr(binding_store, "is_pending", None)
    return bool(is_pending(local_id)) if is_pending is not None else False


def _raw_remote_differs_from_baseline(
    snapshot_entry: Mapping[str, Any], raw_baseline: Mapping[str, Any]
) -> bool:
    return any(
        field in snapshot_entry
        and field in raw_baseline
        and snapshot_entry.get(field) != raw_baseline.get(field)
        for field in _LAGFREE_SCALAR_FIELDS
    )


def _is_ambiguous_snapshot_candidate(
    local_ticket: dict[str, Any],
    snapshot_entry: dict[str, Any],
    raw_baseline: dict[str, Any],
    inbound_mapper: Any,
) -> bool:
    from rebar.reconciler.outbound_field_diff import (
        _local_matches_baseline,
        _remote_matches_baseline,
    )

    canonical_remote = inbound_mapper.map_remote_to_local(snapshot_entry)
    canonical_baseline = inbound_mapper.map_remote_to_local(raw_baseline)
    for field in _CANONICAL_MIRRORED_FIELDS:
        if _local_matches_baseline(
            field,
            local_ticket,
            canonical_baseline,
        ) and not _remote_matches_baseline(field, canonical_remote, canonical_baseline):
            return True
    return False
