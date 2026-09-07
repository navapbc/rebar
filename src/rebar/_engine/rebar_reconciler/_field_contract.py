"""Declare symmetry rules for every Jira-reconciled field.

``FIELD_CONTRACT`` classifies fields as ``bidirectional``, ``add_wins``, or
``one_way_gated``. Bidirectional fields round-trip across canonical codec
subsets, with lossy values declared. Add-wins collections retain values added and
removed in the same pass. One-way-gated fields propagate removals only when
``should_propagate_removal`` confirms ownership.

Property tests require every differ and apply field to be registered. They verify
codec round trips, add-wins behavior, and removal gates against production paths.
Call sites do not depend on this registry.
"""

from __future__ import annotations

from dataclasses import dataclass, field

BIDIRECTIONAL = "bidirectional"
ADD_WINS = "add_wins"
ONE_WAY_GATED = "one_way_gated"

_VALID_CLASSES = frozenset({BIDIRECTIONAL, ADD_WINS, ONE_WAY_GATED})


@dataclass(frozen=True)
class FieldSymmetry:
    """One reconciled field's declared symmetry policy.

    ``conflict_class`` mirrors ``conflict_resolver.FIELD_CLASSES`` (state /
    additive / set) — the CONFLICT axis is a separate, existing registry; the
    parity test keeps the two from drifting.  ``codec`` and ``removal_gate`` are
    dotted references to the module owning the mechanism, for navigation — the
    property tests import and exercise the real thing, never these strings.
    """

    name: str  # local (rebar-side) field name
    symmetry: str  # one of _VALID_CLASSES
    conflict_class: str  # must equal conflict_resolver.FIELD_CLASSES[key]
    outbound_name: str | None = None  # differ's outbound name when it differs
    codec: str | None = None  # value codec reference (docs/navigation)
    removal_gate: str | None = None  # gate reference for one_way_gated removals
    lossy_values: frozenset[str] = field(default_factory=frozenset)
    notes: str = ""


FIELD_CONTRACT: dict[str, FieldSymmetry] = {
    "status": FieldSymmetry(
        name="status",
        symmetry=BIDIRECTIONAL,
        conflict_class="state",
        codec="rebar_reconciler.config.local_to_jira_status/jira_to_local_status",
        lossy_values=frozenset({"blocked", "cancelled", "deleted"}),
        notes=(
            "Forward map is non-injective: blocked→'In Progress', "
            "cancelled/deleted→'Done'. Round-trip identity holds only on the "
            "canonical preimages (idea/open/in_progress/closed); the lossy "
            "values are reconstructed from rebar-status: annotation labels "
            "(never from the workflow status alone — ticket robe-creek-zealot)."
        ),
    ),
    "links": FieldSymmetry(
        name="links",
        symmetry=ONE_WAY_GATED,
        conflict_class="set",
        codec="rebar_reconciler.link_direction (bug 4b59 single source of truth)",
        removal_gate="rebar.reducer._managed_refs.should_propagate_removal",
        notes=(
            "ADD adopts freely both ways; direction (Blocks vs blocked-by → "
            "blocks/depends_on) resolves through link_direction on BOTH the "
            "inbound and the removal path. REMOVE propagates only for links in "
            "managed_refs (outbound_links._diff_link_removals)."
        ),
    ),
    "parent": FieldSymmetry(
        name="parent",
        symmetry=ONE_WAY_GATED,
        conflict_class="state",
        outbound_name="parent",  # inbound differ canonicalizes to parent_id
        removal_gate="rebar.reducer._managed_refs.should_propagate_removal",
        notes=(
            "Outbound 'parent' is the inbound differ's 'parent_id' "
            "(_OUTBOUND_TO_INBOUND_FIELD). Detach propagates only when the "
            "parent ref is managed (outbound_fields/outbound_field_diff gates)."
        ),
    ),
    "title": FieldSymmetry(
        name="title",
        symmetry=BIDIRECTIONAL,
        conflict_class="state",
        outbound_name="summary",
        notes="Jira 'summary' ↔ local 'title'; identity value codec.",
    ),
    "assignee": FieldSymmetry(name="assignee", symmetry=BIDIRECTIONAL, conflict_class="state"),
    "priority": FieldSymmetry(name="priority", symmetry=BIDIRECTIONAL, conflict_class="state"),
    "type": FieldSymmetry(name="type", symmetry=BIDIRECTIONAL, conflict_class="state"),
    "description": FieldSymmetry(
        name="description",
        symmetry=BIDIRECTIONAL,
        conflict_class="additive",
        notes="Conflicts merge additively (resolve_additive), never overwrite.",
    ),
    "comments": FieldSymmetry(
        name="comments",
        symmetry=ADD_WINS,
        conflict_class="additive",
        notes="Append-only stream; nothing ever propagates a comment deletion.",
    ),
    "labels": FieldSymmetry(
        name="labels",
        symmetry=ADD_WINS,
        conflict_class="set",
        codec="local 'tags' ↔ Jira 'labels'",
        notes=(
            "A label in both added and removed within one pass stays ADDED — "
            "the reducer's intra-event TAG_DELTA contract "
            "(rebar.reducer._processors.process_tag_delta), mirrored by the "
            "inbound applier's dedup (apply_inbound_records)."
        ),
    ),
    "watchers": FieldSymmetry(
        name="watchers",
        symmetry=ADD_WINS,
        conflict_class="set",
        notes="Set-valued: conflict resolution unions both sides (no removals).",
    ),
}


def contract_for(field_name: str) -> FieldSymmetry:
    """Return the declared symmetry for ``field_name`` (KeyError = undeclared).

    A KeyError from here in reconciler-adjacent test code is the designed
    failure mode for an unregistered field — declare the field in
    FIELD_CONTRACT rather than catching the error.
    """
    return FIELD_CONTRACT[field_name]


def validate_contract() -> None:
    """Structural self-check: classes valid, gated fields name their gate.

    Raised errors surface at test collection (the property test module calls
    this at import), so a malformed entry breaks the build, not a late test.
    """
    for name, entry in FIELD_CONTRACT.items():
        if entry.name != name:
            raise ValueError(f"FIELD_CONTRACT key {name!r} != entry.name {entry.name!r}")
        if entry.symmetry not in _VALID_CLASSES:
            raise ValueError(f"{name}: unknown symmetry class {entry.symmetry!r}")
        if entry.symmetry == ONE_WAY_GATED and not entry.removal_gate:
            raise ValueError(f"{name}: one_way_gated requires a removal_gate reference")
        if entry.lossy_values and entry.symmetry != BIDIRECTIONAL:
            raise ValueError(f"{name}: lossy_values only applies to bidirectional codecs")
