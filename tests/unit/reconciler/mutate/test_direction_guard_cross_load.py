"""The applier shares one mutation package module. Its direction guard accepts an
equal-valued member of another Enum class, rather than requiring class identity."""

from __future__ import annotations

import enum
import importlib
from types import SimpleNamespace

import pytest


def test_applier_shares_the_one_mutation_module():
    """The applier's mutation seam returns the shared package module, so every
    importer sees one MutationDirection class."""
    applier = importlib.import_module("rebar.reconciler.applier")
    shared = importlib.import_module("rebar.reconciler.mutation")
    assert applier._load_mutation_module() is shared


def test_direction_guard_accepts_an_equal_valued_direction_from_another_enum():
    """_direction_guard compares ``.value``, so a direction member from a
    different enum class with the same value is accepted, not rejected on
    class identity."""
    applier = importlib.import_module("rebar.reconciler.applier")
    applier_mut = applier._load_mutation_module()
    stand_in = enum.Enum("MutationDirection", {"outbound": "outbound", "inbound": "inbound"})
    assert stand_in is not applier_mut.MutationDirection
    mutation = SimpleNamespace(direction=stand_in.outbound)

    applier._direction_guard(mutation, applier_mut.MutationDirection.outbound)


def test_direction_guard_still_rejects_actual_mismatch():
    """Defense-in-depth: a real direction mismatch (inbound passed to outbound
    leaf, or vice versa) MUST still raise — the value-comparison fix must
    not relax the guard."""
    applier = importlib.import_module("rebar.reconciler.applier")
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
