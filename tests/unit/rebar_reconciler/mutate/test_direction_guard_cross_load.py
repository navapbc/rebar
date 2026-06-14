"""Regression test for _direction_guard identity-vs-value bug.

Bug B (discovered during e2e_field_validation_probe run on 2026-05-28):
  The reconciler loads mutation.py multiple times via importlib (once per
  importing module).  Each load produces a distinct MutationDirection enum
  class instance.  The previous _direction_guard used ``is not`` (identity)
  to compare directions, so a Mutation built under load 1 reaching a leaf
  imported under load 2 fired DirectionMismatchError with the paradoxical
  message "leaf expects direction=inbound, got direction=inbound" (both
  values are "inbound" but the enum classes differ).

  The fix compares ``.value`` strings, which are stable across loads.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[4]
APPLIER_PATH = REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "applier.py"
MUTATION_PATH = REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "mutation.py"


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_direction_guard_accepts_mutation_from_separate_module_load():
    """_direction_guard must accept a mutation whose direction enum came from
    a different mutation.py load than the one the applier sees.

    This simulates the production scenario where reconcile.py loads
    mutation.py once and applier.py loads it again — without the fix, the
    two MutationDirection classes are distinct objects and ``is`` returns
    False even when both members share the same ``.value``.
    """
    # Load applier and its bundled mutation module.
    applier = _load_module("applier_dg_cross", APPLIER_PATH)
    applier_mut = applier._load_mutation_module()

    # Load mutation.py separately under a different sys.modules name —
    # this produces a distinct MutationDirection class.
    foreign_mut = _load_module("foreign_mutation_module", MUTATION_PATH)

    # Sanity check: same value, different identity.
    assert applier_mut.MutationDirection.outbound.value == "outbound"
    assert foreign_mut.MutationDirection.outbound.value == "outbound"
    assert applier_mut.MutationDirection is not foreign_mut.MutationDirection, (
        "Test invariant failed — the two loads produced the same class"
    )

    # Build a Mutation using the FOREIGN mutation module.
    mutation = foreign_mut.Mutation(
        direction=foreign_mut.MutationDirection.outbound,
        action=foreign_mut.MutationAction.create,
        target="PROJ-1",
        payload={"summary": "cross-load test"},
        provenance={"source": "test"},
    )

    # The guard should accept this without raising — the fix compares
    # ``.value`` strings rather than identity.
    applier._direction_guard(mutation, applier_mut.MutationDirection.outbound)


def test_direction_guard_still_rejects_actual_mismatch():
    """Defense-in-depth: a real direction mismatch (inbound passed to outbound
    leaf, or vice versa) MUST still raise — the value-comparison fix must
    not relax the guard."""
    applier = _load_module("applier_dg_reject", APPLIER_PATH)
    applier_mut = applier._load_mutation_module()
    errs = applier._load_errors_module()

    # Build an outbound mutation but expect inbound.
    mutation = applier_mut.Mutation(
        direction=applier_mut.MutationDirection.outbound,
        action=applier_mut.MutationAction.create,
        target="PROJ-1",
        payload={"summary": "mismatch test"},
        provenance={"source": "test"},
    )

    with pytest.raises(errs.DirectionMismatchError) as exc_info:
        applier._direction_guard(mutation, applier_mut.MutationDirection.inbound)

    # The error message should NOT be "expects=inbound, got=inbound" — that
    # was the symptom of the original bug.  It should clearly identify the
    # mismatch: expects=inbound, got=outbound.
    msg = str(exc_info.value)
    assert "expects direction=inbound" in msg
    assert "got direction=outbound" in msg
