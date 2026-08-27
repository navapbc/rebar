#!/usr/bin/env python3
"""snapshot_lagfree_refresh.py — refresh scoped keys from the PRIMARY store (bug f449).

Why this exists
---------------
A reconcile pass arbitrates every bound field against ``ctx.curr_snapshot``, which the
fetcher builds from a JQL SEARCH (``fetcher.fetch_snapshot`` -> ``search_issues``). On an
eventually-consistent remote (notably Jira Data Center, whose background Lucene reindex is
unbounded — ADR 0037 s3) that search result LAGS a very recent write.

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
actively-SCOPED bound keys of a pass, ``refresh_scoped_snapshot`` direct-GETs each key and
``overlay_lagfree_scalars`` MERGES the mirrored scalar fields into the existing snapshot
entry — preserving the enrichment (parent / comment / issuelinks) the fetcher layers on
AFTER the base fields (so we merge, never wholesale-replace). The overlay mutates
``ctx.curr_snapshot`` in place before the differs run, so BOTH differs and the later
``_advance_baselines`` (all of which read ``ctx.curr_snapshot``) see the lag-free state.

Scope and cost
--------------
Guarded to SCOPED passes (``selection_ids`` / ``filter_local_ids``) so the direct-GET volume
is bounded by the pass's working set. Full unscoped production passes are left unchanged here
and are covered by a follow-up (ambiguous-candidate-key refresh, ticket 6e5d). A transport
error or a 404 leaves the entry untouched — the pass defers, exactly as the existing
bound-but-absent direct-GET seam does.
"""

from __future__ import annotations

from collections.abc import Iterable
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
    """Bug f449: refresh the actively-scoped bound keys from the primary store.

    Runs at the top of the diff phase — after ``_load_snapshots`` populated
    ``ctx.curr_snapshot`` and ``bind_operation_runtime`` resolved ``ctx.runtime_transport``,
    and before both differs and ``_advance_baselines`` read the snapshot. No-ops for an
    unscoped pass (bounded cost; see follow-up 6e5d) or when no transport is available (a
    partial test ``ctx``). ``ctx`` is the shared ``reconcile._PassContext`` (typed ``Any`` so
    this module holds no import edge back to reconcile.py).
    """
    scoped_ids = getattr(ctx, "selection_ids", None) or getattr(ctx, "filter_local_ids", None)
    if not scoped_ids:
        return
    client = getattr(ctx, "runtime_transport", None)
    if client is None:
        return
    binding_store = getattr(ctx, "binding_store", None)
    curr_snapshot = getattr(ctx, "curr_snapshot", None)
    if binding_store is None or not curr_snapshot:
        return
    scoped_keys = []
    for local_id in scoped_ids:
        jira_key = binding_store.get_jira_key(local_id)
        if jira_key and jira_key in curr_snapshot:
            scoped_keys.append(jira_key)
    if scoped_keys:
        overlay_lagfree_scalars(curr_snapshot, scoped_keys, client)
