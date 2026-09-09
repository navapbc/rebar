"""Verify every registered apply leaf rejects an opposite direction.

Each case builds a valid `Mutation`, flips its frozen direction, and invokes
the matching `_LEAVES` entry. Failures identify the specific direction/action
pair.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
APPLIER_PATH = REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "applier.py"


def _load_applier():
    spec = importlib.util.spec_from_file_location("applier", APPLIER_PATH)
    assert spec is not None and spec.loader is not None
    m = importlib.util.module_from_spec(spec)
    sys.modules["applier"] = m
    spec.loader.exec_module(m)  # type: ignore[union-attr]
    return m


@pytest.fixture(scope="module")
def applier():
    return _load_applier()


def _opposite_direction(applier_mod, direction):
    """Return the inbound/outbound opposite of `direction`."""
    mut_mod = applier_mod._load_mutation_module()
    if direction == mut_mod.MutationDirection.outbound:
        return mut_mod.MutationDirection.inbound
    return mut_mod.MutationDirection.outbound


def _permissive_client():
    """A client whose methods all succeed — the test should fail
    via DirectionMismatchError, not via the underlying client call."""
    return SimpleNamespace(
        create_issue=MagicMock(return_value={"key": "MOCK-1"}),
        update_issue=MagicMock(return_value=None),
        delete_issue=MagicMock(return_value=None),
        remove_label=MagicMock(return_value=None),
        add_label=MagicMock(return_value=None),
        get_issue=MagicMock(return_value={"key": "MOCK-1", "fields": {}}),
    )


def test_every_leaf_raises_direction_mismatch_when_direction_flipped(applier):
    """Call every leaf with the opposite direction and report all failures."""
    mut_mod = applier._load_mutation_module()
    errs = applier._load_errors_module()
    client = _permissive_client()

    # Cross-check: every registered leaf must also be in _VALID_COMBINATIONS.
    valid = mut_mod._VALID_COMBINATIONS
    registry_outside_valid = [
        f"({d.value},{a.value})" for (d, a) in applier._LEAVES.keys() if (d, a) not in valid
    ]
    assert not registry_outside_valid, (
        "applier._LEAVES contains entries not in mutation._VALID_COMBINATIONS: "
        + ", ".join(registry_outside_valid)
    )

    leaf_count = 0
    failures: list[str] = []
    for (direction, action), leaf in applier._LEAVES.items():
        leaf_count += 1

        # Construct a valid Mutation for this leaf. Use a payload broad enough
        # that any leaf-internal validation prior to _direction_guard would not
        # short-circuit — _direction_guard is the first line in every leaf, so
        # this is defense in depth.
        try:
            mutation = mut_mod.Mutation(
                direction=direction,
                action=action,
                target="PROJ-1",
                payload={
                    "labels_to_remove": ["rebar-id-x"],
                    "changed_fields": {"title": "x"},
                    "property": "summary",
                    "value": "x",
                },
                provenance={"source": "test"},
            )
        except Exception as e:  # noqa: BLE001 — assertion that valid Mutation construction raises nothing; any error is recorded as a failure
            failures.append(
                f"({direction.value},{action.value}): "
                f"valid Mutation construction failed: {type(e).__name__}: {e}"
            )
            continue

        # Bypass frozen=True to flip direction. The leaf's _direction_guard
        # must reject this and raise DirectionMismatchError.
        opposite = _opposite_direction(applier, direction)
        object.__setattr__(mutation, "direction", opposite)

        try:
            leaf(mutation, client=client)
            failures.append(
                f"({direction.value},{action.value}): leaf did NOT raise DirectionMismatchError"
            )
        except errs.DirectionMismatchError:
            pass  # expected
        except Exception as e:  # noqa: BLE001 — asserts the leaf raises DirectionMismatchError; any other exception type is recorded as a failure
            failures.append(
                f"({direction.value},{action.value}): "
                f"leaf raised {type(e).__name__} instead of "
                f"DirectionMismatchError: {e}"
            )

    # Sanity floor: _LEAVES is expected to contain ~12 entries.
    assert leaf_count >= 6, (
        f"_LEAVES has only {leaf_count} entries — expected at least 6 "
        "(direction-guard coverage is structurally inadequate)"
    )
    assert not failures, "Direction-guard coverage failures:\n  " + "\n  ".join(failures)
