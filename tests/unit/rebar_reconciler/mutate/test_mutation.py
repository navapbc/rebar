"""RED tests for rebar_reconciler/mutation.py.

Tests assert behavioral contracts for:
  - MutationDirection StrEnum (inbound, outbound)
  - MutationAction StrEnum (create, update, delete, probe, clean_label, repair_property, conflict)
  - Mutation frozen dataclass with __post_init__ validation
  - clean_label and repair_property are inbound-only
  - Mutation is hashable and immutable (frozen=True, slots=True)

These tests are RED: mutation.py does not yet exist.
"""

from __future__ import annotations

import hashlib
import importlib.util
import random
from pathlib import Path
from types import ModuleType

import pytest

# ---------------------------------------------------------------------------
# Module loading helpers
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[4]
MUTATION_PATH = (
    REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "mutation.py"
)


def _load_mutation() -> ModuleType:
    spec = importlib.util.spec_from_file_location("mutation", MUTATION_PATH)
    assert spec is not None and spec.loader is not None, (
        f"Cannot load mutation module from {MUTATION_PATH} — file does not exist yet (RED phase)"
    )
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def mut() -> ModuleType:
    return _load_mutation()


# ---------------------------------------------------------------------------
# Enum membership tests
# ---------------------------------------------------------------------------


def test_action_enum_membership(mut: ModuleType) -> None:
    """MutationAction must contain exactly the 7 specified members."""
    expected = {"create", "update", "delete", "probe", "clean_label", "repair_property", "conflict"}
    actual = {m.value for m in mut.MutationAction}
    assert actual == expected, f"MutationAction members mismatch: {actual} != {expected}"


def test_direction_enum_membership(mut: ModuleType) -> None:
    """MutationDirection must contain exactly inbound and outbound."""
    expected = {"inbound", "outbound"}
    actual = {m.value for m in mut.MutationDirection}
    assert actual == expected, f"MutationDirection members mismatch: {actual} != {expected}"


# ---------------------------------------------------------------------------
# Valid construction tests
# ---------------------------------------------------------------------------


def test_valid_outbound_create(mut: ModuleType) -> None:
    """Mutation with outbound direction and create action must construct without error."""
    m = mut.Mutation(
        direction=mut.MutationDirection.outbound,
        action=mut.MutationAction.create,
        target="issue-123",
        payload={"title": "New Issue"},
        provenance={"source": "reconciler"},
    )
    assert m.direction == mut.MutationDirection.outbound
    assert m.action == mut.MutationAction.create
    assert m.target == "issue-123"


def test_valid_inbound_clean_label(mut: ModuleType) -> None:
    """Mutation with inbound direction and clean_label action must construct without error."""
    m = mut.Mutation(
        direction=mut.MutationDirection.inbound,
        action=mut.MutationAction.clean_label,
        target="issue-456",
        payload={},
        provenance={},
    )
    assert m.direction == mut.MutationDirection.inbound
    assert m.action == mut.MutationAction.clean_label


def test_valid_inbound_repair_property(mut: ModuleType) -> None:
    """Mutation with inbound direction and repair_property action must construct without error."""
    m = mut.Mutation(
        direction=mut.MutationDirection.inbound,
        action=mut.MutationAction.repair_property,
        target="issue-789",
        payload={"field": "status"},
        provenance={"reason": "drift"},
    )
    assert m.action == mut.MutationAction.repair_property


# ---------------------------------------------------------------------------
# Invalid combination tests (inbound-only actions with outbound direction)
# ---------------------------------------------------------------------------


def test_outbound_clean_label_rejected(mut: ModuleType) -> None:
    """Mutation(direction=outbound, action=clean_label) must raise ValueError.

    The error message must mention both 'direction' and 'action' so the
    caller can diagnose the invalid combination.
    """
    with pytest.raises(ValueError) as exc_info:
        mut.Mutation(
            direction=mut.MutationDirection.outbound,
            action=mut.MutationAction.clean_label,
            target="issue-123",
            payload={},
            provenance={},
        )
    error_msg = str(exc_info.value)
    assert "direction" in error_msg, f"ValueError missing 'direction' in: {error_msg!r}"
    assert "action" in error_msg, f"ValueError missing 'action' in: {error_msg!r}"


def test_outbound_repair_property_rejected(mut: ModuleType) -> None:
    """Mutation(direction=outbound, action=repair_property) must raise ValueError.

    The error message must mention both 'direction' and 'action'.
    """
    with pytest.raises(ValueError) as exc_info:
        mut.Mutation(
            direction=mut.MutationDirection.outbound,
            action=mut.MutationAction.repair_property,
            target="issue-123",
            payload={},
            provenance={},
        )
    error_msg = str(exc_info.value)
    assert "direction" in error_msg, f"ValueError missing 'direction' in: {error_msg!r}"
    assert "action" in error_msg, f"ValueError missing 'action' in: {error_msg!r}"


# ---------------------------------------------------------------------------
# Immutability and hashability tests
# ---------------------------------------------------------------------------


def test_mutation_is_frozen_hashable(mut: ModuleType) -> None:
    """Mutation must be frozen (assignment raises FrozenInstanceError) and hashable."""
    m = mut.Mutation(
        direction=mut.MutationDirection.inbound,
        action=mut.MutationAction.create,
        target="issue-001",
        payload={},
        provenance={},
    )
    # Hashable: can be used as a dict key or placed in a set
    s = {m}
    assert m in s

    # Frozen: attribute assignment must raise an error
    with pytest.raises(Exception):  # dataclasses.FrozenInstanceError (subclass of AttributeError)
        m.target = "modified"  # type: ignore[misc]


# ---------------------------------------------------------------------------
# Non-empty target validation test
# ---------------------------------------------------------------------------


def test_empty_target_rejected(mut: ModuleType) -> None:
    """Mutation with an empty target string must raise ValueError."""
    with pytest.raises(ValueError):
        mut.Mutation(
            direction=mut.MutationDirection.outbound,
            action=mut.MutationAction.create,
            target="",
            payload={},
            provenance={},
        )


# ---------------------------------------------------------------------------
# Payload and provenance type tests
# ---------------------------------------------------------------------------


def test_payload_and_provenance_accept_empty_dicts(mut: ModuleType) -> None:
    """Empty dicts for payload and provenance must be accepted."""
    m = mut.Mutation(
        direction=mut.MutationDirection.outbound,
        action=mut.MutationAction.delete,
        target="issue-999",
        payload={},
        provenance={},
    )
    assert m.payload == {}
    assert m.provenance == {}


# ---------------------------------------------------------------------------
# serialize_manifest tests
# ---------------------------------------------------------------------------


def _build_six_mutations(mut: ModuleType) -> list:
    """Build 6 distinct Mutations spanning multiple directions/actions/targets."""
    return [
        mut.Mutation(
            direction=mut.MutationDirection.outbound,
            action=mut.MutationAction.create,
            target="issue-100",
            payload={"title": "A"},
            provenance={"src": "r1"},
        ),
        mut.Mutation(
            direction=mut.MutationDirection.outbound,
            action=mut.MutationAction.update,
            target="issue-200",
            payload={"field": "status"},
            provenance={"src": "r2"},
        ),
        mut.Mutation(
            direction=mut.MutationDirection.inbound,
            action=mut.MutationAction.clean_label,
            target="issue-300",
            payload={},
            provenance={"reason": "drift"},
        ),
        mut.Mutation(
            direction=mut.MutationDirection.inbound,
            action=mut.MutationAction.repair_property,
            target="issue-400",
            payload={"field": "x"},
            provenance={},
        ),
        mut.Mutation(
            direction=mut.MutationDirection.outbound,
            action=mut.MutationAction.delete,
            target="issue-500",
            payload={},
            provenance={"src": "r5"},
        ),
        mut.Mutation(
            direction=mut.MutationDirection.inbound,
            action=mut.MutationAction.probe,
            target="issue-600",
            payload={"probe": True},
            provenance={},
        ),
    ]


def test_serialize_manifest_stable_across_100_permutations(mut: ModuleType) -> None:
    """serialize_manifest must produce identical (json_text, hash) across input orderings."""
    base = _build_six_mutations(mut)
    rng = random.Random(42)
    expected_json, expected_hash = mut.serialize_manifest(base)
    for _ in range(100):
        permuted = base[:]
        rng.shuffle(permuted)
        json_text, sha_hash = mut.serialize_manifest(permuted)
        assert json_text == expected_json
        assert sha_hash == expected_hash


def test_serialize_manifest_sort_key(mut: ModuleType) -> None:
    """serialize_manifest items must be sorted by (direction.value, action.value, target)."""
    import json as _json

    scrambled = [
        mut.Mutation(
            direction=mut.MutationDirection.outbound,
            action=mut.MutationAction.update,
            target="zzz",
            payload={},
            provenance={},
        ),
        mut.Mutation(
            direction=mut.MutationDirection.inbound,
            action=mut.MutationAction.create,
            target="aaa",
            payload={},
            provenance={},
        ),
        mut.Mutation(
            direction=mut.MutationDirection.outbound,
            action=mut.MutationAction.create,
            target="mmm",
            payload={},
            provenance={},
        ),
    ]
    json_text, _ = mut.serialize_manifest(scrambled)
    items = _json.loads(json_text)
    keys = [(it["direction"], it["action"], it["target"]) for it in items]
    assert keys == sorted(keys)
    # Explicit expected order
    assert keys == [
        ("inbound", "create", "aaa"),
        ("outbound", "create", "mmm"),
        ("outbound", "update", "zzz"),
    ]


def test_serialize_manifest_hash_is_sha256_of_text(mut: ModuleType) -> None:
    """The returned hash must be the sha256 hex digest of the utf-8 encoded json_text."""
    muts = _build_six_mutations(mut)
    json_text, sha_hash = mut.serialize_manifest(muts)
    expected = hashlib.sha256(json_text.encode("utf-8")).hexdigest()
    assert sha_hash == expected
    assert len(sha_hash) == 64
    assert all(c in "0123456789abcdef" for c in sha_hash)
