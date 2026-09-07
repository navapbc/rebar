"""Include criterion ``check`` text in ``registry_version``.

Changing free-text checks in ``criteria_routing.json`` must rotate the registry
stamp. Otherwise drift detection would omit part of its declared basis. The
attestation suite separately covers the claim-gate effect of that rotation. The
migration fixture retains one ``success criterion`` phrase so its vocabulary
allowlist remains coupled to registry-version rotation.
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
