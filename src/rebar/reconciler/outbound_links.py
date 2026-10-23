"""Diff local dependencies against canonical remote links.

The injected ``links`` port maps remote links to ``(relation, key, vendor_type)``
and resolves relation payloads. Managed references gate removals; unknown or
never-managed links remain available for inbound adoption. The differ imports
this module without creating a reverse import cycle.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

# ---------------------------------------------------------------------------
# Link diff (story 25ae-92e6-2927-49b6, Cycle 2; canonicalized ticket eefd)
# ---------------------------------------------------------------------------


def _resolve_link(
    relation: str,
    links: Any,
    link_map: Mapping[str, str] | None,
) -> tuple[str, bool] | None:
    """Resolve vendor type and direction for add and remove.

    Overrides replace only the type; the built-in direction remains. ``SKIP``
    or an unmapped relation suppresses both operations.
    """
    from rebar.reconciler.mapping_config import SKIP

    override = link_map.get(relation) if link_map else None
    if override == SKIP:
        return None  # forced skip — neither added nor removed
    payload = links.link_payload_for_relation(relation)
    if isinstance(override, str) and override:
        return (override, payload[1] if payload else False)
    return payload  # built-in (may be None -> skip)


def _diff_links(
    ticket: dict[str, Any],
    jira_fields: dict[str, Any],
    binding_store: Any,
    links: Any,
    link_map: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Emit link adds and managed removals from canonical remote links.

    Adds skip unmapped or unbound targets and deduplicate by vendor type plus
    target key regardless of direction. The payload carries relation, direction
    swap, and link UUID for the applier. Removals require a known, previously
    managed relation absent locally; unknown links remain for inbound adoption.
    """
    deps = ticket.get("deps") or []
    canonical = links.map_remote_links(jira_fields)

    mutations: list[dict[str, Any]] = []
    emitted: set[tuple[str, str]] = set()
    for dep in deps:
        if not isinstance(dep, dict):
            continue
        relation = dep.get("relation")
        if not isinstance(relation, str):
            continue  # malformed dep — no relation to map
        payload = _resolve_link(relation, links, link_map)
        if payload is None:
            continue  # no reliable vendor link type — skip (no-op)
        vendor_type, swap = payload
        target_id = dep.get("target_id")
        if not target_id:
            continue
        target_key = binding_store.get_jira_key(target_id)
        if not target_key:
            continue  # unbound target — skip, retry next pass
        key = (vendor_type, target_key)
        already_present = any(
            other_vendor_type == vendor_type and other_key == target_key
            for _relation, other_key, other_vendor_type in canonical
        )
        if already_present or key in emitted:
            continue  # already present remotely (either direction) or already queued
        emitted.add(key)
        mutations.append(
            {
                "action": "add",
                "type": vendor_type,
                "to_key": target_key,
                "relation": relation,
                "swap": swap,
                "link_uuid": dep.get("link_uuid"),
            }
        )
    # Only a managed local unlink becomes a remove; the applier resolves its link id.
    mutations.extend(_diff_link_removals(ticket, jira_fields, binding_store, links, link_map))
    return mutations


def _diff_link_removals(
    ticket: dict[str, Any],
    jira_fields: dict[str, Any],
    binding_store: Any,
    links: Any,
    link_map: Mapping[str, str] | None = None,
) -> list[dict[str, Any]]:
    """Remove only canonical links absent locally and present in managed refs.

    The injected port has already resolved direction. Imports stay local so
    standalone-loaded differ tests do not require the full backend.
    """
    from collections import Counter

    from rebar.reconciler.mapping_config import SKIP
    from rebar.reducer._managed_refs import should_propagate_removal

    canonical = links.map_remote_links(jira_fields)
    if not canonical:
        return []
    get_local_id = getattr(binding_store, "get_local_id", None)
    if get_local_id is None:
        return []  # cannot resolve targets -> fail-open (no removal, additive-only)

    # Recover overridden types absent from the built-in reverse map. Ambiguous
    # vendor types remain unmapped so removal never guesses a relation.
    _vendor_counts = Counter(v for v in (link_map or {}).values() if v != SKIP)
    reverse = {v: k for k, v in (link_map or {}).items() if v != SKIP and _vendor_counts[v] == 1}

    local_deps: set[tuple[str, str]] = set()
    for d in ticket.get("deps") or []:
        if not isinstance(d, dict):
            continue
        d_relation = d.get("relation")
        d_target = d.get("target_id")
        if d_relation and d_target:
            local_deps.add((d_relation, d_target))

    removals: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for relation, other_key, opaque_vendor_type in canonical:
        if relation is None:
            # The built-in reverse did not know this remote type — recover it from the
            # config overlay. Still None -> genuinely unmapped, left for inbound ADOPT.
            relation = reverse.get(opaque_vendor_type)
            if relation is None:
                continue
        if _resolve_link(relation, links, link_map) is None:
            continue  # relation is skipped — neither added nor removed
        local_target = get_local_id(other_key)
        if not local_target:
            continue  # unbound — retry next pass
        if (relation, local_target) in local_deps:
            continue  # still linked locally — not a removal
        if not should_propagate_removal(relation, local_target, ticket):
            continue  # never managed this link — adopt inbound, do not clobber
        dedup_key = (opaque_vendor_type, other_key)
        if dedup_key in seen:
            continue
        seen.add(dedup_key)
        removals.append(
            {
                "action": "remove",
                "type": opaque_vendor_type,
                "to_key": other_key,
                "relation": relation,
            }
        )
    return removals
