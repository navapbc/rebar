"""Parametrized DirectionMismatchError tests across every _LEAVES entry (2f51 DD remediation).

For each (direction, action) pair registered in applier._LEAVES, this test:
1. Constructs a valid Mutation with the leaf's expected direction.
2. Flips direction to the opposite via object.__setattr__ (frozen dataclass bypass).
3. Invokes the leaf and asserts DirectionMismatchError is raised.

The production _direction_guard is wired identically on all 12 leaves; this
test is structural coverage to satisfy the per-leaf DD requirement from 2f51.
"""
from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
APPLIER_PATH = REPO_ROOT / "src" / "rebar" / "_engine" / "dso_reconciler" / "applier.py"


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
    """Smoke-loop: every leaf must raise DirectionMismatchError when its
    Mutation's direction is set to the opposite of the leaf's expectation.

    Implemented as a single test (not @pytest.mark.parametrize) so the
    fixture loads applier exactly once and so a single test name documents
    "all leaves covered" per the 2f51 DD. Per-pair failures are collected
    into a single failures list and reported via one assert at the end so
    that a leaf with a missing or wrong-direction guard is clearly attributed.
    """
    mut_mod = applier._load_mutation_module()
    errs = applier._load_errors_module()
    client = _permissive_client()

    # Cross-check: every registered leaf must also be in _VALID_COMBINATIONS.
    valid = mut_mod._VALID_COMBINATIONS
    registry_outside_valid = [
        f"({d.value},{a.value})"
        for (d, a) in applier._LEAVES.keys()
        if (d, a) not in valid
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
                    "labels_to_remove": ["dso-id-x"],
                    "changed_fields": {"title": "x"},
                    "property": "summary",
                    "value": "x",
                },
                provenance={"source": "test"},
            )
        except Exception as e:
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
                f"({direction.value},{action.value}): "
                f"leaf did NOT raise DirectionMismatchError"
            )
        except errs.DirectionMismatchError:
            pass  # expected
        except Exception as e:
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
