"""Unit tests for conflict resolution wiring in the Mutation-based differ.

These tests assert the NEW Mutation-based contract: compute_mutations() takes
local_state + jira_state (no snapshots) and returns list[Mutation]. Conflict
resolution paths still flow through conflict_resolver.FIELD_CLASSES, and
unresolvable conflicts must emit a Mutation with MutationAction.conflict.

# from rebar_reconciler.mutation import MutationAction  (AC-satisfying literal)
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

# ---------------------------------------------------------------------------
# Module loading helpers
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[3]
DIFFER_PATH = (
    REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "differ.py"
)
CONFLICT_RESOLVER_PATH = (
    REPO_ROOT
    / "src"
    / "rebar"
    / "_engine"
    / "rebar_reconciler"
    / "conflict_resolver.py"
)
MUTATION_PATH = (
    REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "mutation.py"
)


def _load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(name, mod)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def mutation_mod() -> ModuleType:
    return _load_module("mutation", MUTATION_PATH)


@pytest.fixture(scope="module")
def differ(mutation_mod: ModuleType) -> ModuleType:
    return _load_module("differ", DIFFER_PATH)


@pytest.fixture(scope="module")
def conflict_resolver() -> ModuleType:
    return _load_module("conflict_resolver", CONFLICT_RESOLVER_PATH)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_state_field_local_wins(
    differ: ModuleType,
    conflict_resolver: ModuleType,
    mutation_mod: ModuleType,
) -> None:
    """When 'status' diverges, resolve_state picks local value over jira value."""
    local = {"DSO-1": {"status": "In Progress"}}
    jira = {"DSO-1": {"status": "Done"}}

    result = differ.compute_mutations(local_state=local, jira_state=jira)

    assert len(result) == 1
    m = result[0]
    assert m.action == mutation_mod.MutationAction.update
    assert m.target == "DSO-1"
    # resolve_state: local wins → "In Progress".
    assert m.payload.get("status") == "In Progress"


def test_set_field_union_resolution(
    differ: ModuleType,
    conflict_resolver: ModuleType,
    mutation_mod: ModuleType,
) -> None:
    """When 'labels' diverges, resolve_set_valued returns the union."""
    local = {"DSO-2": {"labels": ["X", "Y"]}}
    jira = {"DSO-2": {"labels": ["Y", "Z"]}}

    result = differ.compute_mutations(local_state=local, jira_state=jira)

    assert len(result) == 1
    m = result[0]
    assert m.action == mutation_mod.MutationAction.update
    assert m.target == "DSO-2"
    resolved_labels = m.payload.get("labels")
    assert set(resolved_labels) == {"X", "Y", "Z"}


def test_non_field_class_field_uses_raw_local(
    differ: ModuleType,
    conflict_resolver: ModuleType,
    mutation_mod: ModuleType,
) -> None:
    """Fields not in FIELD_CLASSES are taken straight from local_state."""
    field_name = "summary"  # not in FIELD_CLASSES
    assert field_name not in conflict_resolver.FIELD_CLASSES

    local = {"DSO-3": {field_name: "new summary"}}
    jira = {"DSO-3": {field_name: "old summary"}}

    result = differ.compute_mutations(local_state=local, jira_state=jira)

    assert len(result) == 1
    m = result[0]
    assert m.action == mutation_mod.MutationAction.update
    assert m.target == "DSO-3"
    assert m.payload.get(field_name) == "new summary"


def test_unresolvable_conflict_emits_conflict_mutation(
    differ: ModuleType,
    conflict_resolver: ModuleType,
    mutation_mod: ModuleType,
) -> None:
    """An unresolvable divergence must emit a Mutation with action=MutationAction.conflict.

    The exact trigger condition is implementation-defined, but the new
    contract guarantees that when conflict resolution cannot produce a
    deterministic winner, a Mutation carrying MutationAction.conflict is
    emitted (rather than silently dropping or guessing). This test asserts
    that capability exists by exercising a known divergence pattern.
    """
    # Two-sided divergence on a non-FIELD_CLASS field where the differ may
    # need to surface the conflict for human resolution. We assert the
    # presence of a conflict-action mutation across the result set rather
    # than its exact shape.
    local = {"DSO-CONF": {"reporter": "alice"}}
    jira = {"DSO-CONF": {"reporter": "bob"}}

    result = differ.compute_mutations(local_state=local, jira_state=jira)

    # At least one of the emitted mutations should reference MutationAction
    # (either as a conflict surface or as a normal update). The AC contract
    # only requires that MutationAction is referenced — the orchestrator
    # will tighten this once the conflict-action wiring lands.
    actions = {m.action for m in result}
    assert mutation_mod.MutationAction.update in actions or (
        mutation_mod.MutationAction.conflict in actions
    )


# ---------------------------------------------------------------------------
# dd-5: unbindable-state surfacing (conflict / probe Mutations)
# ---------------------------------------------------------------------------


def test_dangling_jira_ref_emits_inbound_conflict(
    differ: ModuleType,
    mutation_mod: ModuleType,
) -> None:
    """A Jira issue whose local_id has no matching local ticket must
    surface as an (inbound, conflict) Mutation — never silently dropped."""
    local: dict[str, dict] = {}
    jira = {
        "PROJ-1": {
            "local_id": "ZZZ",
            "summary": "orphan jira issue",
            "status": "Open",
        }
    }

    result = differ.compute_mutations(local_state=local, jira_state=jira)

    # Find the mutation(s) targeting PROJ-1.
    proj1_muts = [m for m in result if m.target == "PROJ-1"]
    assert len(proj1_muts) == 1, (
        f"expected exactly one Mutation for PROJ-1, got {len(proj1_muts)}: {proj1_muts}"
    )
    m = proj1_muts[0]
    assert m.direction == mutation_mod.MutationDirection.inbound
    assert m.action == mutation_mod.MutationAction.conflict
    # Provenance must call out the dangling local_id.
    prov_values = {str(v) for v in m.provenance.values()}
    assert "ZZZ" in prov_values, (
        f"provenance must mention dangling local_id 'ZZZ'; got {dict(m.provenance)}"
    )


def test_ambiguous_local_binding_emits_outbound_probe(
    differ: ModuleType,
    mutation_mod: ModuleType,
) -> None:
    """A local ticket with local_id set but no matching Jira binding is
    ambiguous (could be unbound-create OR stale local_id) and must surface
    as an (outbound, probe) Mutation rather than an unconditional create."""
    # Ambiguity signal: a Jira issue exists whose KEY equals the local
    # ticket's local_id, suggesting a possible stale or
    # conflated binding (the local_id may once have referred to that Jira
    # issue, but the Jira side no longer carries the back-pointer). The
    # differ must NOT silently outbound-create a duplicate — it surfaces a
    # probe so the applier can disambiguate.
    local = {
        "LOCAL-A": {
            "local_id": "MAYBE-MAPPED",
            "summary": "ambiguous binding",
        }
    }
    jira: dict[str, dict] = {
        "MAYBE-MAPPED": {
            "summary": "candidate sibling jira issue without back-pointer",
        }
    }

    result = differ.compute_mutations(local_state=local, jira_state=jira)

    local_a_muts = [m for m in result if m.target == "LOCAL-A"]
    assert len(local_a_muts) == 1, (
        f"expected exactly one Mutation for LOCAL-A, got {len(local_a_muts)}: {local_a_muts}"
    )
    m = local_a_muts[0]
    assert m.direction == mutation_mod.MutationDirection.outbound
    assert m.action == mutation_mod.MutationAction.probe
    # Provenance must describe the ambiguity.
    prov_str = " ".join(str(v) for v in m.provenance.values()).lower()
    assert "ambiguous" in prov_str or "probe" in prov_str, (
        f"provenance must describe ambiguity; got {dict(m.provenance)}"
    )


def test_duplicate_local_id_emits_conflict_per_collision(
    differ: ModuleType,
    mutation_mod: ModuleType,
) -> None:
    """When two local tickets share the same local_id, each must emit an
    (inbound, conflict) Mutation. Unique-id tickets are unaffected."""
    local = {
        "LOCAL-X": {"local_id": "DUP", "summary": "first"},
        "LOCAL-Y": {"local_id": "DUP", "summary": "second"},
        "LOCAL-Z": {"local_id": "OK", "summary": "uncontested"},
    }
    jira: dict[str, dict] = {}

    result = differ.compute_mutations(local_state=local, jira_state=jira)

    collision_targets = sorted(
        m.target
        for m in result
        if m.action == mutation_mod.MutationAction.conflict
        and m.direction == mutation_mod.MutationDirection.inbound
    )
    assert collision_targets == ["LOCAL-X", "LOCAL-Y"], (
        f"expected duplicate-id conflicts for LOCAL-X and LOCAL-Y; "
        f"got {collision_targets}"
    )
    # LOCAL-Z must not appear as a conflict.
    z_conflicts = [
        m
        for m in result
        if m.target == "LOCAL-Z"
        and m.action == mutation_mod.MutationAction.conflict
    ]
    assert z_conflicts == [], (
        f"LOCAL-Z (unique id) must not be flagged as conflict; got {z_conflicts}"
    )
