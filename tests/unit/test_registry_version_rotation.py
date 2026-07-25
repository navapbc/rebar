"""`registry_version` is sensitive to a criterion's `check` text (ticket 2aa6).

This story renames the pooled "acceptance/success criterion" vocabulary inside
`criteria_routing.json`'s free-text `check` strings. Those strings are part of the
hashed basis, so the rename rotates the `regver` stamp.

Pinned here: the rotation is REAL. If editing a `check` string did NOT change the
stamp, the routing index would not be part of the basis it claims to cover, and
drift detection would be silently blind to exactly this kind of edit.

(That the rotation is HARMLESS at the claim gate is ADR 0053 / ticket 1f32, already
covered by the attestation-validity suite; this story only depends on it.)
"""

from __future__ import annotations

import copy
from typing import Any

from rebar.llm.plan_review import registry
from rebar.llm.plan_review.manifest import registry_version


def _mutate_first_check(obj: Any) -> bool:
    """Append a marker to the first `check` string found; True if one was mutated."""
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key == "check" and isinstance(value, str):
                obj[key] = value + " (perturbed)"
                return True
            if _mutate_first_check(value):
                return True
    elif isinstance(obj, list):
        return any(_mutate_first_check(item) for item in obj)
    return False


def test_registry_version_rotates_when_a_check_string_changes(monkeypatch) -> None:
    """Both stamps are computed at run time; neither is hard-coded."""
    before = registry_version(None)

    mutated = copy.deepcopy(registry._routing_index())
    assert _mutate_first_check(mutated), "no `check` string found in the routing index"
    monkeypatch.setattr(registry, "_routing_index", lambda: mutated)

    after = registry_version(None)

    assert before, "registry_version returned an empty stamp"
    assert before != after, "editing a `check` string did not rotate the registry stamp"


def test_registry_version_is_deterministic() -> None:
    """Guards the comparison above: a stable stamp means inequality is real signal."""
    assert registry_version(None) == registry_version(None)
