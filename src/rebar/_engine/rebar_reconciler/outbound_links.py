"""Outbound link-diff cluster for bidirectional Jira sync.

The local-deps → Jira-issuelinks seam extracted from ``outbound_differ.py`` (it
grew past the module-size soft cap). Owns the ADD+REMOVE link diff
(``_diff_links``) and the managed-ref-gated REMOVE pass (``_diff_link_removals``).

Ticket eefd: the diff now compares in CANONICAL (relation) shape rather than raw
Jira shape, so this module imports NOTHING from ``adapters.jira``. The two
translations that used to live here — the existing-issuelinks index and the
relation<->Jira-link-type vocabulary lookup — moved onto the injected
``SupportsLinks`` capability (a Backend-port object, e.g. ``JiraBackend``), passed
to ``_diff_links``/``_diff_link_removals`` as a new 4th positional argument
``links``:

  * ``links.map_remote_links(jira_fields)`` — the canonical link set: entries
    ``(relation, remote_key, opaque_vendor_type)``.
  * ``links.link_payload_for_relation(relation)`` — ``(opaque_vendor_type, swap)``
    or ``None`` for an unmapped relation.

``compute_outbound_mutations`` (in ``outbound_differ``) imports this module; the
dependency is one-way (this module imports nothing from ``outbound_differ``),
avoiding an import cycle.
"""

from __future__ import annotations

from typing import Any

# ---------------------------------------------------------------------------
# Link diff (story 25ae-92e6-2927-49b6, Cycle 2; canonicalized ticket eefd)
# ---------------------------------------------------------------------------


def _diff_links(
    ticket: dict[str, Any],
    jira_fields: dict[str, Any],
    binding_store: Any,
    links: Any,
) -> list[dict[str, Any]]:
    """Compare a local ticket's ``deps`` to its Jira issuelinks. Emits ADDs and REMOVEs.

    ``links`` is the injected ``SupportsLinks`` capability (ticket eefd) — the two
    link translations (canonical link set + relation->vendor-type payload) are its
    responsibility; this module never names a vendor link-type string.

    ADD pass — for each local dep ``{target_id, relation, link_uuid}``:
      - skip if the relation has no vendor payload (``links.link_payload_for_relation``
        returns ``None`` — duplicates / supersedes / discovered_from);
      - resolve ``target_id`` -> Jira key (skip unbound, mirroring the
        parent-unbound skip in ``_map_local_to_jira_fields``);
      - DEDUP DIRECTION-AGNOSTICALLY against the canonical link set: an ADD is
        skipped when the set already has an entry whose ``opaque_vendor_type``
        equals this dep's vendor type AND whose ``remote_key`` equals the target
        key — preserving the former ``_existing_jira_links`` semantics exactly (a
        vendor link between the local issue and K dedups a local dep to K
        regardless of which side K sits on). This is intentionally NOT deduped on
        ``relation`` (that would change behavior);
      - emit ``{"action":"add","type":...,"to_key":...,"relation":...,
        "swap":...,"link_uuid":...}`` — payload keys/values unchanged.

    REMOVE pass (wake-inn-parse) — see :func:`_diff_link_removals`: a canonical-set
    entry with a known ``relation``, absent locally, that we MANAGED (in
    ``managed_refs``) emits ``{"action":"remove","type":...,"to_key":...,
    "relation":...}`` so a deliberate local unlink propagates instead of being
    re-added inbound every pass. A never-managed or unmapped (``relation is None``)
    link is left for inbound ADOPT, never clobbered.

    The applier consumes ``to_key`` as the link target (resolving the concrete link id
    for a REMOVE at apply time). The recorded ``relation`` is the rebar relation;
    ``swap`` is handled by the applier when issuing the directional vendor call.
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
        payload = links.link_payload_for_relation(relation)
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
    # Symmetric REMOVE pass (wake-inn-parse): a remote link of a mapped relation,
    # absent locally, that we MANAGED is a deliberate local unlink — propagate the
    # delete so the inbound differ stops re-adding it (the churn). A never-managed
    # link is left for inbound ADOPT, never clobbered. The applier resolves the link
    # id at apply time (mirrors the ADD dedup probe), so we emit only (type, to_key).
    mutations.extend(_diff_link_removals(ticket, jira_fields, binding_store, links))
    return mutations


def _diff_link_removals(
    ticket: dict[str, Any],
    jira_fields: dict[str, Any],
    binding_store: Any,
    links: Any,
) -> list[dict[str, Any]]:
    """Emit managed-ref-gated link REMOVE mutations (the symmetric half of _diff_links).

    For each canonical-set entry with a known ``relation`` whose ``(relation,
    local_target)`` is NOT in the local deps but IS in the ticket's ``managed_refs``,
    emit ``{"action":"remove","type":...,"to_key":...,"relation":...}``. Direction
    (inward vs outward — Blocks into blocks/depends_on) is already resolved by
    ``links.map_remote_links``, mirroring the inbound differ. Local import keeps the
    differ free of module-scope heavy imports (standalone-loaded in tests)."""
    from rebar.reducer._managed_refs import should_propagate_removal

    canonical = links.map_remote_links(jira_fields)
    if not canonical:
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
    for relation, other_key, opaque_vendor_type in canonical:
        # An unmapped vendor link type (relation is None) is never managed by us —
        # left for inbound ADOPT.
        if relation is None:
            continue
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
