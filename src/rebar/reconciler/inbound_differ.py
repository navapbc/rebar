"""Compare Jira snapshots with bound local tickets and emit inbound mutations.
Local changes win same-pass conflicts. Unbound Jira issues are skipped for
outbound creation. This differ uses the binding interface and injected backend
mappers."""

from __future__ import annotations

from dataclasses import dataclass
from dataclasses import field as dataclass_field
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

# Re-export collection differs for existing module attributes and local calls.
from rebar.reconciler.inbound_collection_diffs import (  # noqa: F401
    _EXCLUDED_PREFIXES,
    RECONCILER_MARKER,
    _diff_comments_inbound,
    _diff_labels_inbound,
    _diff_link_removals_inbound,
    _diff_links_inbound,
    _load_link_direction,
)

# Re-export field helpers for existing callers and config parity tests.
# The vendor mapper comes through the injected Backend port.
from rebar.reconciler.inbound_fields import (  # noqa: F401
    _ADF_KEY_INBOUND,
    _JIRA_TO_LOCAL_PRIORITY,
    _JIRA_TO_LOCAL_STATUS,
    _JIRA_TO_LOCAL_TYPE,
    _assignee_matches,
    _description_forms,
    _extract_jira_field_value,
    _load_adf,
    _normalize_jira_body,
    classify_inbound_parent,
    diff_inbound_parent,
)

if TYPE_CHECKING:
    from rebar.reconciler._backend import InboundMapper, OutboundMapper


# ---------------------------------------------------------------------------
# BindingStore protocol — codes against PR #401's interface
# ---------------------------------------------------------------------------


@runtime_checkable
class BindingStoreProtocol(Protocol):
    """Minimal interface for the inbound binding store lookup."""

    def get_local_id(self, jira_key: str) -> str | None: ...


# ---------------------------------------------------------------------------
# InboundMutation dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InboundMutation:
    """A single inbound change to apply to the local ticket system."""

    jira_key: str
    local_id: str
    action: str  # "update"
    fields: dict[str, Any]  # changed fields only
    comments: list[dict[str, Any]] = dataclass_field(default_factory=list)
    labels: list[dict[str, Any]] = dataclass_field(default_factory=list)
    links: list[dict[str, Any]] = dataclass_field(default_factory=list)


def _diff_jira_vs_local(
    jira_fields: dict[str, Any],
    local_ticket: dict[str, Any],
    binding_store: Any = None,
    *,
    inbound_mapper: InboundMapper | None = None,
    outbound_mapper: OutboundMapper | None = None,
) -> dict[str, Any]:
    """Return mapped Jira fields that differ from the local ticket.
    With a binding store, resolve Jira's parent to a local ID. Defer an unbound
    parent key so a later pass can retry."""
    # Direct callers without an injected mapper use the configured backend.
    if inbound_mapper is None:
        from rebar.config import compose_config
        from rebar.reconciler._backend_registry import select_backend

        inbound_mapper = select_backend(compose_config()).inbound
    jira_mapped = inbound_mapper.map_remote_to_local(jira_fields)
    changed: dict[str, Any] = {}

    # Exclude ticket_type. Jira's coarser types cannot safely overwrite richer
    # local types such as epic.
    field_map = {
        "title": "title",
        "description": "description",
        "priority": "priority",
        "status": "status",
        "assignee": "assignee",
    }

    for local_field, ticket_field in field_map.items():
        # Compare the raw Jira assignee with local email, name, or account ID.
        # Comparing only the mapped display name would emit repeated false changes.
        if local_field == "assignee":
            local_assignee = local_ticket.get(ticket_field) or ""
            if not _assignee_matches(local_assignee, jira_fields.get("assignee")):
                changed[local_field] = jira_mapped.get(local_field)
                # Forward the opaque identity with the scalar so inbound EDIT can mint a
                # ghost user. The event writer selects fields explicitly.
                if "assignee_identity" in jira_mapped:
                    changed["assignee_identity"] = jira_mapped["assignee_identity"]
            continue
        if local_field == "status" and "status" not in jira_mapped:
            continue  # Bug 5886: unmapped Jira status → leave local status untouched.
        jira_val = jira_mapped.get(local_field)
        local_val = local_ticket.get(ticket_field)
        # Normalise None to empty string for string fields
        if jira_val is None:
            jira_val = "" if local_field not in ("priority",) else 2
        if local_val is None:
            local_val = "" if local_field not in ("priority",) else 2
        # Compare descriptions after the outbound ADF fit and wrap normalization.
        # This prevents Jira's truncated or joined echo from replacing full local prose.
        # Keep the fetched Jira value for remote edits. Normalization affects only
        # the comparison copy.
        jira_emit = jira_val
        if local_field == "description" and isinstance(local_val, str):
            forms = _description_forms(
                local_val, jira_val, jira_emit, local_ticket, outbound_mapper
            )
            if forms is None:
                continue
            local_val, jira_val = forms
        # Ignore trailing whitespace removed by Jira's ADF round trip.
        if (
            isinstance(local_val, str)
            and isinstance(jira_val, str)
            and local_val.rstrip() == jira_val.rstrip()
        ):
            continue
        # Keep a locally archived or deleted ticket terminal on Jira's closed echo.
        if (
            local_field == "status"
            and local_val in ("archived", "deleted")
            and jira_val == "closed"
        ):
            continue
        if jira_val != local_val:
            changed[local_field] = jira_emit

    # Delegate observed, unresolved, and managed parent changes to inbound_fields.
    # The legacy call without a binding store skips parent sync.
    if binding_store is not None:
        # A parent clear requires evidence of a former peer parent. Legacy stores
        # without this lookup cannot authorize a clear.
        _get_peer_parent = getattr(binding_store, "get_peer_parent", None)
        _peer_parent = (
            _get_peer_parent(local_ticket.get("ticket_id") or "")
            if _get_peer_parent is not None
            else None
        )
        emit_parent, parent_value = diff_inbound_parent(
            jira_fields, local_ticket, binding_store.get_local_id, _peer_parent
        )
        if emit_parent:
            changed["parent_id"] = parent_value

    return changed


# Canonicalize outbound Jira field names for inbound same-pass suppression.
# Map parent to parent_id and summary to title. Add entries and field contract
# tests whenever the 2 directions use different names for a new field.
_OUTBOUND_TO_INBOUND_FIELD: dict[str, str] = {
    "parent": "parent_id",
    "summary": "title",
}


def _build_outbound_context(
    outbound_mutations: list[Any] | None,
) -> dict[str, dict[str, Any]]:
    """Index outbound fields, label actions, and link actions by Jira key.
    Inbound suppression uses this index because both differs read the same
    pre-pass snapshot. Canonical field names prevent local changes from being
    reversed by a stale Jira echo."""
    ctx: dict[str, dict[str, Any]] = {}
    if not outbound_mutations:
        return ctx
    for om in outbound_mutations:
        jira_key = getattr(om, "jira_key", None)
        if not jira_key:
            continue
        entry = ctx.setdefault(
            jira_key,
            {
                "label_adds": set(),
                "label_removes": set(),
                "fields": set(),
                "link_add_keys": set(),
                "link_remove_keys": set(),
            },
        )
        for lk in getattr(om, "links", []) or []:
            if not isinstance(lk, dict):
                continue
            action = lk.get("action")
            to_key = lk.get("to_key")
            if action == "add" and to_key:
                entry["link_add_keys"].add(to_key)
            elif action == "remove" and to_key:
                # wake-inn-parse: an outbound link REMOVE (a deliberate local unlink) must
                # suppress the inbound link ADD that would re-reflect the still-present Jira
                # link this pass — remove-wins, so local wins and the unlink converges.
                entry["link_remove_keys"].add(to_key)
        for lm in getattr(om, "labels", []) or []:
            action = lm.get("action") if isinstance(lm, dict) else None
            label = lm.get("label") if isinstance(lm, dict) else None
            if not label:
                continue
            if action == "add":
                entry["label_adds"].add(label)
            elif action == "remove":
                entry["label_removes"].add(label)
        for field_name in (getattr(om, "fields", {}) or {}).keys():
            # Store inbound field names so parent and title updates suppress stale
            # same-pass Jira echoes.
            entry["fields"].add(_OUTBOUND_TO_INBOUND_FIELD.get(field_name, field_name))
    return ctx


def _resolve_backend_mappers(
    inbound_mapper: InboundMapper | None,
    outbound_mapper: OutboundMapper | None,
) -> tuple[InboundMapper, OutboundMapper]:
    """Use injected mappers where supplied and fill either missing mapper from
    the configured backend. This keeps vendor selection outside the differ."""
    if inbound_mapper is not None and outbound_mapper is not None:
        return inbound_mapper, outbound_mapper
    from rebar.config import compose_config
    from rebar.reconciler._backend_registry import select_backend

    backend = select_backend(compose_config())
    return (inbound_mapper or backend.inbound), (outbound_mapper or backend.outbound)


def compute_inbound_mutations(
    jira_snapshot: dict[str, dict[str, Any]],
    binding_store: BindingStoreProtocol,
    local_tickets_by_id: dict[str, dict[str, Any]],
    outbound_mutations: list[Any] | None = None,
    *,
    inbound_mapper: InboundMapper | None = None,
    outbound_mapper: OutboundMapper | None = None,
) -> tuple[list[InboundMutation], int]:
    """Return inbound changes and the number of suppressed items for bound tickets.
    Skip unbound Jira keys and missing local tickets. Outbound changes win
    same-pass field, label, and link conflicts because both differs read the
    pre-pass snapshot. Comments remain inbound. Without outbound mutations,
    suppression_count is zero."""
    # Use injected Backend mappers or resolve missing ones from the configured
    # backend for direct callers.
    inbound_mapper, outbound_mapper = _resolve_backend_mappers(inbound_mapper, outbound_mapper)

    mutations: list[InboundMutation] = []
    outbound_ctx = _build_outbound_context(outbound_mutations)
    suppression_count = 0

    for jira_key, jira_fields in sorted(jira_snapshot.items()):
        local_id = binding_store.get_local_id(jira_key)
        if local_id is None:
            # Unbound Jira issue — skip (local is source of truth)
            continue

        local_ticket = local_tickets_by_id.get(local_id)
        if local_ticket is None:
            # Bound but local ticket missing — skip (may be deleted locally)
            continue

        changed = _diff_jira_vs_local(
            jira_fields,
            local_ticket,
            binding_store=binding_store,
            inbound_mapper=inbound_mapper,
            outbound_mapper=outbound_mapper,
        )
        label_mutations = _diff_labels_inbound(jira_fields, local_ticket)
        comment_mutations = _diff_comments_inbound(jira_fields, local_ticket)
        link_mutations = _diff_links_inbound(
            jira_fields, local_ticket, binding_store, local_tickets_by_id
        )
        # Ticket 2b16: the symmetric REMOVE half — a local dep whose peer link was deleted.
        # Guarded (G1 observed / G2 bound / G3 managed); see _diff_link_removals_inbound.
        link_mutations.extend(_diff_link_removals_inbound(jira_fields, local_ticket, binding_store))

        # Bidirectional suppression (bug 3bf8): filter out inbound mutations
        # that would clobber a just-emitted outbound change for this target.
        ob_entry = outbound_ctx.get(jira_key)
        if ob_entry is not None:
            # Scalar fields: drop any inbound field update where outbound
            # is updating the same field.
            if changed:
                pre_field_count = len(changed)
                changed = {k: v for k, v in changed.items() if k not in ob_entry["fields"]}
                suppression_count += pre_field_count - len(changed)
            # Labels: drop inbound ADD when outbound REMOVES the same label,
            # and inbound REMOVE when outbound ADDS the same label.
            if label_mutations:
                filtered_labels: list[dict[str, Any]] = []
                for lm in label_mutations:
                    action = lm.get("action")
                    label = lm.get("label")
                    if action == "add" and label in ob_entry["label_removes"]:
                        suppression_count += 1
                        continue
                    if action == "remove" and label in ob_entry["label_adds"]:
                        suppression_count += 1
                        continue
                    filtered_labels.append(lm)
                label_mutations = filtered_labels
            # Map each local target ID to its Jira key before action-aware same-pass
            # link suppression. A conflicting outbound action takes precedence.
            if link_mutations and (ob_entry["link_add_keys"] or ob_entry["link_remove_keys"]):
                _get_jira_key = getattr(binding_store, "get_jira_key", None)
                _suppress_by_action: dict[str, set[str]] = {
                    # Suppress inbound adds that echo an outbound add or oppose a local unlink.
                    "add": ob_entry["link_add_keys"] | ob_entry["link_remove_keys"],
                    # G4 — THE GUARD THAT COVERS G3's BLIND SPOT, not a cosmetic one. A ref is
                    # "managed" the instant it is created LOCALLY (add_managed_ref is folded by
                    # the LINK-event processor), so G3 cannot tell a brand-new local dep from a
                    # peer-deleted one, and the peer legitimately has no such link yet. In a
                    # healthy pass outbound emits a link ADD for exactly that dep, so an
                    # outbound ADD to this target means "being pushed", NOT "peer deleted it" —
                    # never mirror a removal for it. An outbound REMOVE for the same pair also
                    # suppresses: the two directions must not both act on one pair in a single
                    # pass, and deferring re-evaluates next pass rather than double-removing.
                    "remove": ob_entry["link_add_keys"] | ob_entry["link_remove_keys"],
                }
                filtered_links: list[dict[str, Any]] = []
                for lk in link_mutations:
                    target_key = (
                        _get_jira_key(lk.get("target_id")) if _get_jira_key is not None else None
                    )
                    _keys = _suppress_by_action.get(str(lk.get("action")), set())
                    if target_key and target_key in _keys:
                        suppression_count += 1
                        continue
                    filtered_links.append(lk)
                link_mutations = filtered_links

        if changed or label_mutations or comment_mutations or link_mutations:
            mutations.append(
                InboundMutation(
                    jira_key=jira_key,
                    local_id=local_id,
                    action="update",
                    fields=changed,
                    labels=label_mutations,
                    comments=comment_mutations,
                    links=link_mutations,
                )
            )

    return mutations, suppression_count
