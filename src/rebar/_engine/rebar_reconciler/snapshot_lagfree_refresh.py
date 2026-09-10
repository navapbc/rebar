#!/usr/bin/env python3
"""snapshot_lagfree_refresh.py — refresh clobber-risk keys from the PRIMARY store.

Why this exists
---------------
A reconcile pass arbitrates every bound field against ``ctx.curr_snapshot``, which the
fetcher builds from a JQL SEARCH (``fetcher.fetch_snapshot`` -> ``search_issues``). On an
eventually-consistent remote — notably Jira Data Center, whose background Lucene reindex
is unbounded (ADR 0085 §3; see also ``docs/adr/0055-jira-family-sub-seam.md``'s
"Search-index lag" row) — that search result LAGS a very recent write.

The bound-field INBOUND differ is the exposed consumer. ``inbound_differ._diff_jira_vs_local``
is LEVEL-triggered and consults NO baseline: it emits an inbound mirror for any mirrored
scalar where the snapshot value differs from local. Right after rebar pushes a field, local
equals the advanced baseline (ADR 0026 / bug e6e9), so the OUTBOUND differ suppresses that
field — which means the same-pass bidirectional suppression (bug 3bf8) does NOT fire for it.
If the echo pass then reads a STALE snapshot (the field still shows its pre-push value), the
inbound differ sees ``snapshot(OLD) != local(NEW)`` and mirrors OLD back over local: a clobber
of rebar's own just-synced write. A ``remote != baseline`` guard cannot fix this — the
baseline is NEW while the stale search still shows OLD, so ``OLD != NEW`` still fires. The
only robust fix is to arbitrate on LAG-FREE remote state.

What this module does
---------------------
``get_issue_by_rest`` is a primary-store GET (immediately consistent, no index lag). For the
actively-SCOPED bound keys of a pass — plus the ambiguous bound keys in an unscoped pass
where local still equals the baseline but the search snapshot does not — ``refresh_scoped_snapshot``
direct-GETs each key and ``overlay_lagfree_scalars`` MERGES the mirrored scalar fields into
the existing snapshot entry — preserving the enrichment (parent / comment / issuelinks) the
fetcher layers on AFTER the base fields (so we merge, never wholesale-replace). The overlay mutates
``ctx.curr_snapshot`` in place before the differs run, so BOTH differs and the later
``_advance_baselines`` (all of which read ``ctx.curr_snapshot``) see the lag-free state.

Scope and cost
--------------
Scoped passes (``selection_ids`` / ``filter_local_ids``) refresh only their working set.
Full unscoped production passes refresh only ambiguous clobber-risk candidates: confirmed
bindings present in the fetched snapshot whose local mirrored scalar is unchanged since its
baseline while the search snapshot's value differs from that baseline. That keeps the
direct-GET volume bounded to the risk set rather than every binding. A transport error or
a 404 leaves the entry untouched — the pass defers, exactly as the existing bound-but-absent
direct-GET seam does.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ._backend import TicketTransport

# The five inbound-mirrored scalar fields (outbound_field_diff._INBOUND_MIRRORED_FIELDS),
# named in the RAW vendor shape the snapshot and a direct GET both use. These are the only
# keys the overlay copies from the lag-free GET; every other key (enrichment: parent,
# comment, issuelinks, labels, ...) is left as the search snapshot produced it.
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
    """Merge lag-free mirrored scalars from a direct GET into the snapshot entries.

    For each ``jira_key`` PRESENT in ``curr_snapshot``, direct-GET the issue's raw fields via
    the classified seam (``outbound_differ._safe_get_issue`` -> ``get_issue_by_rest``) and,
    on success, copy the ``_LAGFREE_SCALAR_FIELDS`` present in the fresh fields over the
    snapshot entry — mutating ``curr_snapshot`` in place and preserving all other (enrichment)
    keys. A transport error or a 404 (both surfaced by ``_safe_get_issue`` as a non-dict
    sentinel) leaves that entry untouched so the pass defers it.

    Returns the number of entries actually refreshed (a GET that returned real fields).
    """
    # Lazy sibling import (the package's by-path load convention keeps module load order
    # free of a hard edge; _safe_get_issue owns the HTTPError/URLError classification).
    from rebar_reconciler.outbound_differ import _safe_get_issue

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
    """Refresh clobber-risk bound keys from the primary store.

    Runs at the top of the diff phase — after ``_load_snapshots`` populated
    ``ctx.curr_snapshot`` and ``bind_operation_runtime`` resolved ``ctx.runtime_transport``,
    and before both differs and ``_advance_baselines`` read the snapshot. Scoped passes
    refresh the scoped key set; unscoped passes refresh only ambiguous keys where a stale
    search result could trigger an inbound clobber. No-ops when no transport is available
    (a partial test ``ctx``). ``ctx`` is the shared ``reconcile._PassContext`` (typed
    ``Any`` so this module holds no import edge back to reconcile.py).
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
    all_bindings = getattr(binding_store, "all_bindings", None)
    get_baseline = getattr(binding_store, "get_baseline", None)
    backend = getattr(ctx, "runtime_backend", None)
    inbound_mapper = getattr(backend, "inbound", None)
    if all_bindings is None or get_baseline is None or inbound_mapper is None:
        return []
    local_by_id = {
        t.get("ticket_id", t.get("id", "")): t
        for t in (getattr(ctx, "local_tickets", None) or [])
        if isinstance(t, dict)
    }
    candidates = []
    for local_id, entry in all_bindings().items():
        if not isinstance(entry, Mapping) or entry.get("state") != "confirmed":
            continue
        jira_key = entry.get("jira_key")
        local_ticket = local_by_id.get(local_id)
        raw_baseline = get_baseline(local_id)
        if not jira_key or jira_key not in curr_snapshot or not local_ticket or not raw_baseline:
            continue
        if _is_ambiguous_snapshot_candidate(
            local_ticket,
            curr_snapshot[jira_key],
            raw_baseline,
            inbound_mapper,
        ):
            candidates.append(jira_key)
    return candidates


def _is_ambiguous_snapshot_candidate(
    local_ticket: dict[str, Any],
    snapshot_entry: dict[str, Any],
    raw_baseline: dict[str, Any],
    inbound_mapper: Any,
) -> bool:
    from rebar_reconciler.outbound_field_diff import (
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
