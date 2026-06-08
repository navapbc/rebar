"""Mutation manifest types for the dso_reconciler.

Defines the immutable Mutation value object and its enum vocabulary
(MutationDirection, MutationAction). Direction/action validity is enforced
by an explicit allowlist (`_VALID_COMBINATIONS`): clean_label and
repair_property are inbound-only — they have no outbound semantics.
"""

import hashlib
import json
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class MutationDirection(StrEnum):
    inbound = "inbound"
    outbound = "outbound"


class MutationAction(StrEnum):
    create = "create"
    update = "update"
    delete = "delete"
    probe = "probe"
    clean_label = "clean_label"
    repair_property = "repair_property"
    conflict = "conflict"


# All (direction, action) pairs except outbound-with-inbound-only actions.
_INBOUND_ONLY_ACTIONS: frozenset[MutationAction] = frozenset(
    {MutationAction.clean_label, MutationAction.repair_property}
)

_VALID_COMBINATIONS: frozenset[tuple[MutationDirection, MutationAction]] = frozenset(
    (direction, action)
    for direction in MutationDirection
    for action in MutationAction
    if not (direction is MutationDirection.outbound and action in _INBOUND_ONLY_ACTIONS)
)


@dataclass(frozen=True, slots=True, eq=False)
class Mutation:
    """An immutable description of a single reconciler-driven change."""

    direction: MutationDirection
    action: MutationAction
    target: str
    payload: Mapping[str, Any]
    provenance: Mapping[str, Any]

    def __post_init__(self) -> None:
        if not isinstance(self.target, str) or not self.target:
            raise ValueError("target must be a non-empty str")
        if not isinstance(self.payload, Mapping):
            raise TypeError("payload must be a Mapping")
        if not isinstance(self.provenance, Mapping):
            raise TypeError("provenance must be a Mapping")
        if (self.direction, self.action) not in _VALID_COMBINATIONS:
            raise ValueError(
                f"invalid (direction={self.direction.value}, "
                f"action={self.action.value}) combination"
            )

    def __eq__(self, other: object) -> bool:
        # Identity is the (direction, action, target) triple.  payload and
        # provenance are descriptive metadata and are intentionally excluded
        # so that __eq__ is consistent with __hash__ (payload/provenance are
        # often dict-valued and therefore unhashable).
        if not isinstance(other, Mutation):
            return NotImplemented
        return (
            self.direction == other.direction
            and self.action == other.action
            and self.target == other.target
        )

    def __hash__(self) -> int:
        # payload/provenance are Mapping (often dict, which is unhashable).
        # Identity of a Mutation for set/dict-key purposes is the
        # (direction, action, target) triple — payload/provenance are
        # descriptive metadata, not part of the identity.
        return hash((self.direction, self.action, self.target))


def serialize_manifest(mutations: Iterable[Mutation]) -> tuple[str, str]:
    """Serialize a list of Mutations to a canonical JSON manifest + sha256 hash.

    Sort by (direction.value, action.value, target). Pure — no I/O, no time.
    Returns (json_text, sha256_hash_hex).
    """
    sorted_muts = sorted(
        mutations, key=lambda m: (m.direction.value, m.action.value, m.target)
    )
    items = [
        {
            "direction": m.direction.value,
            "action": m.action.value,
            "target": m.target,
            "payload": dict(m.payload),
            "provenance": dict(m.provenance),
        }
        for m in sorted_muts
    ]
    json_text = json.dumps(items, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    sha256_hash = hashlib.sha256(json_text.encode("utf-8")).hexdigest()
    return json_text, sha256_hash
