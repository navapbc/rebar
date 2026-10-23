"""Exercise the reconciler's field-symmetry mechanisms (e931).

Collection requires every handled field in FIELD_CONTRACT. Conflict classes
must match the resolver. Check status round trips and exact declared lossy
residue, inverse link directions, add-wins replay, and managed-only removal
gates. These properties catch registry omissions and contract drift."""

from __future__ import annotations

import importlib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
_ENGINE = REPO_ROOT / "src" / "rebar" / "reconciler"


_contract = importlib.import_module("rebar.reconciler._field_contract")
_config = importlib.import_module("rebar.reconciler.config")
_link_direction = importlib.import_module("rebar.reconciler.link_direction")
_conflict_resolver = importlib.import_module("rebar.reconciler.conflict_resolver")
_inbound_differ = importlib.import_module("rebar.reconciler.inbound_differ")

from rebar.reducer._managed_refs import should_propagate_removal  # noqa: E402
from rebar.reducer._processors import process_tag_delta  # noqa: E402

FIELD_CONTRACT = _contract.FIELD_CONTRACT


# ---------------------------------------------------------------------------
# 1. Import-time coverage guard — an undeclared handled field fails COLLECTION.
# ---------------------------------------------------------------------------
def _assert_registry_covers_handled_fields() -> None:
    _contract.validate_contract()
    handled = set(_conflict_resolver.FIELD_CLASSES)
    declared = set(FIELD_CONTRACT)
    missing = handled - declared
    if missing:
        raise AssertionError(
            f"reconciled fields handled by conflict_resolver.FIELD_CLASSES but "
            f"undeclared in _field_contract.FIELD_CONTRACT: {sorted(missing)} — "
            f"declare each field's symmetry class (see _field_contract.py docstring)"
        )
    outbound_names = {e.outbound_name for e in FIELD_CONTRACT.values() if e.outbound_name}
    uncovered = set(_inbound_differ._OUTBOUND_TO_INBOUND_FIELD) - outbound_names
    if uncovered:
        raise AssertionError(
            f"outbound field names canonicalized by inbound_differ but not declared "
            f"as any FIELD_CONTRACT entry's outbound_name: {sorted(uncovered)}"
        )


_assert_registry_covers_handled_fields()


def test_registry_covers_all_handled_fields() -> None:
    """Readable mirror of the import-time guard (the guard is the enforcer)."""
    _assert_registry_covers_handled_fields()


# ---------------------------------------------------------------------------
# 2. Conflict-class parity — the symmetry and conflict axes must not drift.
# ---------------------------------------------------------------------------
def test_conflict_class_parity_with_field_classes() -> None:
    for name, cls in _conflict_resolver.FIELD_CLASSES.items():
        assert FIELD_CONTRACT[name].conflict_class == cls, (
            f"{name}: registry conflict_class={FIELD_CONTRACT[name].conflict_class!r} "
            f"!= conflict_resolver.FIELD_CLASSES {cls!r}"
        )


def test_core_field_classes_pinned() -> None:
    """Pin the load-bearing symmetry declarations BY NAME (mutation check #1)."""
    c = _contract
    assert FIELD_CONTRACT["status"].symmetry == c.BIDIRECTIONAL
    assert FIELD_CONTRACT["status"].lossy_values == {"blocked", "cancelled", "deleted"}
    assert FIELD_CONTRACT["labels"].symmetry == c.ADD_WINS
    assert FIELD_CONTRACT["links"].symmetry == c.ONE_WAY_GATED
    assert FIELD_CONTRACT["parent"].symmetry == c.ONE_WAY_GATED
    for gated in ("links", "parent"):
        assert FIELD_CONTRACT[gated].removal_gate == (
            "rebar.reducer._managed_refs.should_propagate_removal"
        )


# ---------------------------------------------------------------------------
# 3a. bidirectional(status): round-trip through the REAL config maps.
# ---------------------------------------------------------------------------
def test_status_roundtrip_on_canonical_preimages() -> None:
    lossy = FIELD_CONTRACT["status"].lossy_values
    fwd = _config.local_to_jira_status
    rev = _config.jira_to_local_status
    for local in fwd:
        if local in lossy:
            continue
        assert rev[fwd[local]] == local, (
            f"declared-roundtrip status {local!r} does not survive "
            f"local→jira→local ({local!r}→{fwd[local]!r}→{rev[fwd[local]]!r})"
        )


def test_status_lossy_residue_exactly_declared() -> None:
    """The DECLARED lossy set equals the COMPUTED non-injective residue.

    Both directions: a value that stops round-tripping must be declared (no
    silent loss), and a declared-lossy value that actually round-trips is an
    over-declaration (stale contract).  The residue is computed from the real
    maps, so editing config.py without updating the contract fails here.
    """
    fwd = _config.local_to_jira_status
    rev = _config.jira_to_local_status
    residue = {local for local in fwd if rev.get(fwd[local]) != local}
    assert residue == FIELD_CONTRACT["status"].lossy_values


def test_status_lossy_values_do_not_roundtrip() -> None:
    """Contrast case (non-vacuity): each lossy value really is lossy."""
    fwd = _config.local_to_jira_status
    rev = _config.jira_to_local_status
    for local in FIELD_CONTRACT["status"].lossy_values:
        assert rev[fwd[local]] != local


# ---------------------------------------------------------------------------
# 3b. links direction codec through the REAL link_direction module (bug 4b59).
# ---------------------------------------------------------------------------
def test_inverse_relation_is_an_involution() -> None:
    inv = _link_direction.INVERSE_RELATION
    for relation, inverse in inv.items():
        assert inv[inverse] == relation


def test_jira_link_types_map_to_known_relations() -> None:
    known = {"blocks", "depends_on", "relates_to"}
    assert set(_link_direction.JIRA_LINK_TO_RELATION.values()) <= known


def test_resolve_inbound_link_resolves_direction_through_real_function() -> None:
    outward = {"type": {"name": "Blocks"}, "outwardIssue": {"key": "J-2"}}
    inward = {"type": {"name": "Blocks"}, "inwardIssue": {"key": "J-1"}}
    assert _link_direction.resolve_inbound_link(outward) == ("J-2", "blocks")
    assert _link_direction.resolve_inbound_link(inward) == ("J-1", "depends_on")


# ---------------------------------------------------------------------------
# 3c. add_wins(labels): the reducer's intra-event TAG_DELTA contract.
# ---------------------------------------------------------------------------
def test_add_wins_tag_in_both_added_and_removed_stays_added() -> None:
    state: dict = {"tags": []}
    process_tag_delta(state, {"added": ["x"], "removed": ["x"]})
    assert "x" in state["tags"]


def test_add_wins_replay_idempotent() -> None:
    state: dict = {"tags": ["keep"]}
    delta = {"added": ["x"], "removed": ["keep"]}
    process_tag_delta(state, delta)
    once = list(state["tags"])
    process_tag_delta(state, delta)
    assert state["tags"] == once == ["x"]


def test_add_wins_plain_remove_still_removes() -> None:
    """Add-wins is intra-event only — an uncontested remove must apply."""
    state: dict = {"tags": ["y"]}
    process_tag_delta(state, {"added": [], "removed": ["y"]})
    assert "y" not in state["tags"]


# ---------------------------------------------------------------------------
# 3d. one_way_gated(links/parent): the REAL removal gate, fail-open.
# ---------------------------------------------------------------------------
def test_gate_propagates_only_managed_refs() -> None:
    ticket = {"managed_refs": [["parent", "T-1"], ["blocks", "T-2"]]}
    assert should_propagate_removal("parent", "T-1", ticket) is True
    assert should_propagate_removal("blocks", "T-2", ticket) is True
    assert should_propagate_removal("blocks", "T-9", ticket) is False  # unmanaged


def test_gate_fails_open_without_managed_refs() -> None:
    assert should_propagate_removal("parent", "T-1", {}) is False
    assert should_propagate_removal("parent", "T-1", {"managed_refs": "junk"}) is False


def test_gate_rejects_unmanaged_kinds() -> None:
    ticket = {"managed_refs": [["parent", "T-1"]]}
    assert should_propagate_removal("watchers", "T-1", ticket) is False


# ---------------------------------------------------------------------------
# 4. Name-canonicalization coverage matches the differ's map exactly.
# ---------------------------------------------------------------------------
def test_outbound_name_canonicalization_matches_differ_map() -> None:
    differ_map = _inbound_differ._OUTBOUND_TO_INBOUND_FIELD
    assert differ_map == {"parent": "parent_id", "summary": "title"}
    assert FIELD_CONTRACT["parent"].outbound_name == "parent"
    assert FIELD_CONTRACT["title"].outbound_name == "summary"
