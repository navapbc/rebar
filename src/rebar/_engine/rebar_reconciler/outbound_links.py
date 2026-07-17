"""Outbound link-diff cluster for bidirectional Jira sync.

The local-deps → Jira-issuelinks seam extracted from ``outbound_differ.py`` (it
grew past the module-size soft cap). Owns the relation↔Jira-link-type vocabulary,
the existing-issuelinks index (``_existing_jira_links``), the ADD+REMOVE link
diff (``_diff_links``), and the managed-ref-gated REMOVE pass
(``_diff_link_removals``).

``compute_outbound_mutations`` (in ``outbound_differ``) imports this module; the
dependency is one-way (this module imports nothing from ``outbound_differ``),
avoiding an import cycle.
"""

from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# Link diff (story 25ae-92e6-2927-49b6, Cycle 2)
# ---------------------------------------------------------------------------
#
# Relation <-> Jira link-type mapping. The canonical definition lives in
# acli_graph._RELATION_TO_JIRA_LINK (Cycle 1), but the differ is loaded
# standalone via spec_from_file_location in tests (no package context, so
# ``from rebar_reconciler.acli_graph import ...`` is not reliably importable
# and would pull the whole ACLI client import chain). We re-declare a local
# copy here — the same single-source-of-vocabulary pattern as the local
# _LOCAL_TO_JIRA_* constants above. Keep in sync with acli_graph.
#
# Each entry maps a rebar relation -> (jira_link_type, swap_endpoints).
# ``swap_endpoints`` records that "A relation B" maps to a Jira link with the
# endpoints reversed: "A depends_on B" == "B blocks A". Relations with no
# reliable Jira link type (duplicates / supersedes / discovered_from) are
# intentionally ABSENT and SKIPPED by the differ.
_RELATION_TO_JIRA_LINK: dict[str, tuple[str, bool]] = {
    "blocks": ("Blocks", False),
    "depends_on": ("Blocks", True),  # A depends_on B == B blocks A
    "relates_to": ("Relates", False),
}

# Reverse of the above for the REMOVE pass (wake-inn-parse): a Jira link type maps
# to a base rebar relation; the inward/outward direction disambiguates Blocks into
# blocks vs depends_on (mirrors inbound_differ._JIRA_LINK_TO_RELATION). Re-declared
# locally for the same standalone-load reason as _RELATION_TO_JIRA_LINK above.
_JIRA_LINK_TO_RELATION: dict[str, str] = {
    "Blocks": "blocks",
    "Relates": "relates_to",
}

# Inverse of a directional rebar relation (blocks<->depends_on; symmetric relations
# invert to themselves via ``.get(rel, rel)``). MIRRORS
# inbound_differ._INVERSE_RELATION — both the inbound ADD path and this REMOVE path
# must disambiguate a Jira Blocks link by direction the SAME way, or a managed
# unlink computes the wrong relation and silently fails the managed_refs gate. The
# two copies are pinned together by the live-ground-truth
# test_link_direction_absolute.py (bug 4b59 / epic 58b0).
_INVERSE_RELATION: dict[str, str] = {"blocks": "depends_on", "depends_on": "blocks"}


def _existing_jira_links(jira_fields: dict[str, Any]) -> set[tuple[str, str]]:
    """Index a Jira issue's ``issuelinks`` as a ``{(type_name, target_key)}`` set.

    Direction semantics (verified live): for the issue X carrying this
    ``issuelinks`` array, an entry with ``inwardIssue.key == Y`` names X as the
    OUTWARD (e.g. blocker) side and Y the inward side; an entry with
    ``outwardIssue.key == Y`` names Y the outward side. The dedup key we build is
    ``(type_name, the-other-issue-key)`` REGARDLESS of direction — an ADD-only
    outbound diff just needs to know "does a link of this type to that key
    already exist in either direction", which is what avoids per-pass churn.
    """
    existing: set[tuple[str, str]] = set()
    for link in jira_fields.get("issuelinks") or []:
        if not isinstance(link, dict):
            continue
        link_type = link.get("type") or {}
        type_name = link_type.get("name") if isinstance(link_type, dict) else None
        if not type_name:
            continue
        for side_key in ("inwardIssue", "outwardIssue"):
            side = link.get(side_key)
            if isinstance(side, dict):
                side_key_val = side.get("key")
                if side_key_val:
                    existing.add((type_name, side_key_val))
    return existing


def _diff_links(
    ticket: dict[str, Any],
    jira_fields: dict[str, Any],
    binding_store: Any,
) -> list[dict[str, Any]]:
    """Compare a local ticket's ``deps`` to its Jira issuelinks. Emits ADDs and REMOVEs.

    ADD pass — for each local dep ``{target_id, relation, link_uuid}``:
      - resolve ``target_id`` -> Jira key (skip unbound, mirroring the
        parent-unbound skip in ``_map_local_to_jira_fields``);
      - map ``relation`` -> Jira link type via ``_RELATION_TO_JIRA_LINK``
        (skip unmapped relations: duplicates / supersedes / discovered_from);
      - DEDUP against the issue's existing ``issuelinks`` by
        ``(jira_link_type, target_key)`` so an already-present link emits
        nothing (critical to avoid re-emitting a `set_relationship` every pass);
      - emit ``{"action":"add","type":...,"to_key":...,"relation":...,
        "link_uuid":...}``.

    REMOVE pass (wake-inn-parse) — see :func:`_diff_link_removals`: a Jira link of a
    mapped type, absent locally, that we MANAGED (in ``managed_refs``) emits
    ``{"action":"remove","type":...,"to_key":...,"relation":...}`` so a deliberate local
    unlink propagates instead of being re-added inbound every pass. A never-managed Jira
    link is left for inbound ADOPT, never clobbered.

    The applier consumes ``to_key`` as the link target (resolving the concrete link id
    for a REMOVE at apply time). The recorded ``relation`` is the rebar relation;
    ``swap_endpoints`` is handled by the applier when issuing the directional Jira call.
    """
    deps = ticket.get("deps") or []
    existing = _existing_jira_links(jira_fields)

    mutations: list[dict[str, Any]] = []
    emitted: set[tuple[str, str]] = set()
    for dep in deps:
        if not isinstance(dep, dict):
            continue
        relation = dep.get("relation")
        if not isinstance(relation, str):
            continue  # malformed dep — no relation to map
        mapped = _RELATION_TO_JIRA_LINK.get(relation)
        if mapped is None:
            continue  # no reliable Jira link type — skip (no-op)
        jira_type, swap = mapped
        target_id = dep.get("target_id")
        if not target_id:
            continue
        target_key = binding_store.get_jira_key(target_id)
        if not target_key:
            continue  # unbound target — skip, retry next pass
        key = (jira_type, target_key)
        if key in existing or key in emitted:
            continue  # already present in Jira (either direction) or already queued
        emitted.add(key)
        mutations.append(
            {
                "action": "add",
                "type": jira_type,
                "to_key": target_key,
                "relation": relation,
                "swap": swap,
                "link_uuid": dep.get("link_uuid"),
            }
        )
    # Symmetric REMOVE pass (wake-inn-parse): a Jira link of a mapped type, absent
    # locally, that we MANAGED is a deliberate local unlink — propagate the delete so
    # the inbound differ stops re-adding it (the churn). A never-managed Jira link is
    # left for inbound ADOPT, never clobbered. The applier resolves the link id at
    # apply time (mirrors the ADD dedup probe), so we emit only (type, to_key).
    mutations.extend(_diff_link_removals(ticket, jira_fields, binding_store))
    return mutations


def _diff_link_removals(
    ticket: dict[str, Any],
    jira_fields: dict[str, Any],
    binding_store: Any,
) -> list[dict[str, Any]]:
    """Emit managed-ref-gated link REMOVE mutations (the symmetric half of _diff_links).

    For each Jira issuelink of a mapped type whose ``(relation, local_target)`` is NOT in
    the local deps but IS in the ticket's ``managed_refs``, emit
    ``{"action":"remove","type":...,"to_key":...,"relation":...}``. Direction (inward vs
    outward) disambiguates Blocks into blocks/depends_on, mirroring the inbound differ.
    Local import keeps the differ free of module-scope heavy imports (standalone-loaded in
    tests)."""
    from rebar.reducer._managed_refs import should_propagate_removal

    issuelinks = jira_fields.get("issuelinks") or []
    if not isinstance(issuelinks, list) or not issuelinks:
        return []
    get_local_id = getattr(binding_store, "get_local_id", None)
    if get_local_id is None:
        return []  # cannot resolve targets -> fail-open (no removal, additive-only)

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
    for link in issuelinks:
        if not isinstance(link, dict):
            continue
        link_type = link.get("type") or {}
        type_name = link_type.get("name") if isinstance(link_type, dict) else None
        if not type_name:
            continue  # no link-type name — never managed by us
        base_relation = _JIRA_LINK_TO_RELATION.get(type_name)
        if base_relation is None:
            continue  # link type with no rebar relation mapping — never managed by us
        inward = link.get("inwardIssue")
        outward = link.get("outwardIssue")
        inward_key = inward.get("key") if isinstance(inward, dict) else None
        outward_key = outward.get("key") if isinstance(outward, dict) else None
        # LIVE-JIRA direction (bug 4b59, mirrors inbound_differ._resolve_inbound_link):
        # outwardIssue "blocks" -> base relation; inwardIssue "is blocked by" -> inverse.
        if outward_key:
            other_key = outward_key
            relation = base_relation
        elif inward_key:
            other_key = inward_key
            relation = _INVERSE_RELATION.get(base_relation, base_relation)
        else:
            continue
        local_target = get_local_id(other_key)
        if not local_target:
            continue  # unbound — retry next pass
        if (relation, local_target) in local_deps:
            continue  # still linked locally — not a removal
        if not should_propagate_removal(relation, local_target, ticket):
            continue  # never managed this link — adopt inbound, do not clobber
        dedup_key = (type_name, other_key)
        if dedup_key in seen:
            continue
        seen.add(dedup_key)
        removals.append(
            {"action": "remove", "type": type_name, "to_key": other_key, "relation": relation}
        )
    return removals
