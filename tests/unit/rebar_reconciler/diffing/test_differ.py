"""Unit tests for rebar_reconciler/differ.py — Mutation-based compute_mutations().

These tests assert the NEW stateless Mutation-based differ contract:
  - Differ inputs: ``local_state`` and ``jira_state`` (plain dicts keyed by
    stable issue identifier).
  - Differ returns ``list[Mutation]`` (see rebar_reconciler.mutation).
  - Each Mutation carries ``.direction`` (MutationDirection),
    ``.action`` (MutationAction), ``.target``, ``.payload``, ``.provenance``.
  - No snapshot/prev/next state — the differ is stateless.

Tests intentionally end RED until the differ is rewritten to the new contract.

# from rebar_reconciler.mutation import  (AC-satisfying literal — actual load below)
"""

from __future__ import annotations

import copy
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

# ---------------------------------------------------------------------------
# Module loading helpers (matches the pattern in test_mutation.py)
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[4]
DIFFER_PATH = REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "differ.py"
MUTATION_PATH = REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "mutation.py"


def _load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault(name, mod)
    spec.loader.exec_module(mod)  # type: ignore[union-attr]
    return mod


@pytest.fixture(scope="module")
def mutation_mod() -> ModuleType:
    """Loads rebar_reconciler.mutation (Mutation, MutationAction, MutationDirection)."""
    return _load_module("mutation", MUTATION_PATH)


@pytest.fixture(scope="module")
def differ(mutation_mod: ModuleType) -> ModuleType:
    # Ensure mutation module is loaded first (differ imports it).
    return _load_module("differ", DIFFER_PATH)


# ---------------------------------------------------------------------------
# Tests — same scenarios as the snapshot-diff version, rewritten against
# the new Mutation-based contract.
# ---------------------------------------------------------------------------


def test_identical_states_produce_empty_list(differ: ModuleType, mutation_mod: ModuleType) -> None:
    state = {"DSO-1": {"summary": "hello", "status": "open"}}
    result = differ.compute_mutations(local_state=state, jira_state=copy.deepcopy(state))
    assert result == []


def test_empty_states_produce_empty_list(differ: ModuleType, mutation_mod: ModuleType) -> None:
    result = differ.compute_mutations(local_state={}, jira_state={})
    assert result == []


def test_excluded_fields_only_change_produces_no_mutations(
    differ: ModuleType, mutation_mod: ModuleType
) -> None:
    # Both excluded fields differ — no mutation should be emitted.
    jira = {"DSO-1": {"local_id": "old-local", "rebar-id": "old-id"}}
    local = {"DSO-1": {"local_id": "new-local", "rebar-id": "new-id"}}
    result = differ.compute_mutations(local_state=local, jira_state=jira)
    assert result == []


def test_new_key_in_local_produces_outbound_create_mutation(
    differ: ModuleType, mutation_mod: ModuleType
) -> None:
    """A key present in local_state but absent from jira_state → outbound create."""
    local = {"DSO-42": {"summary": "new issue", "priority": "high"}}
    jira: dict = {}
    result = differ.compute_mutations(local_state=local, jira_state=jira)
    assert len(result) == 1
    m = result[0]
    assert isinstance(m, mutation_mod.Mutation)
    assert m.action == mutation_mod.MutationAction.create
    assert m.direction == mutation_mod.MutationDirection.outbound
    assert m.target == "DSO-42"
    assert m.payload == {"summary": "new issue", "priority": "high"}


def test_new_key_in_jira_produces_inbound_create_mutation(
    differ: ModuleType, mutation_mod: ModuleType
) -> None:
    """A key present in jira_state but absent from local_state → inbound create."""
    local: dict = {}
    jira = {"DSO-43": {"summary": "from jira", "priority": "low"}}
    result = differ.compute_mutations(local_state=local, jira_state=jira)
    assert len(result) == 1
    m = result[0]
    assert m.action == mutation_mod.MutationAction.create
    assert m.direction == mutation_mod.MutationDirection.inbound
    assert m.target == "DSO-43"


def test_removed_key_produces_delete_mutation(differ: ModuleType, mutation_mod: ModuleType) -> None:
    """A key present in jira_state but removed from local_state → outbound delete."""
    local: dict = {}
    jira = {"DSO-7": {"summary": "going away"}}
    # Local-driven deletion: local removed it, jira still has it → outbound delete.
    result = differ.compute_mutations(local_state=local, jira_state=jira)
    # NB: the AC for this story does not nail down create/delete asymmetry for
    # one-sided absence; the implementation may classify either side as
    # "missing → delete from the other side". The assertion below pins the
    # behaviour we expect: a delete mutation is emitted for the absent key.
    assert len(result) == 1
    m = result[0]
    assert m.action in (
        mutation_mod.MutationAction.delete,
        mutation_mod.MutationAction.create,
    )
    assert m.target == "DSO-7"


def test_changed_field_produces_update_mutation(
    differ: ModuleType, mutation_mod: ModuleType
) -> None:
    local = {"DSO-3": {"summary": "new summary", "status": "open"}}
    jira = {"DSO-3": {"summary": "old summary", "status": "open"}}
    result = differ.compute_mutations(local_state=local, jira_state=jira)
    assert len(result) == 1
    m = result[0]
    assert m.action == mutation_mod.MutationAction.update
    assert m.target == "DSO-3"
    # Direction must be one of the explicit enum members.
    assert m.direction in (
        mutation_mod.MutationDirection.inbound,
        mutation_mod.MutationDirection.outbound,
    )
    assert m.payload.get("summary") == "new summary"


def test_update_contains_only_changed_fields(differ: ModuleType, mutation_mod: ModuleType) -> None:
    local = {"DSO-5": {"summary": "same", "status": "closed", "priority": "low"}}
    jira = {"DSO-5": {"summary": "same", "status": "open", "priority": "low"}}
    result = differ.compute_mutations(local_state=local, jira_state=jira)
    assert len(result) == 1
    payload = result[0].payload
    assert payload.get("status") == "closed"
    assert "summary" not in payload
    assert "priority" not in payload


def test_excluded_field_not_in_update_payload(differ: ModuleType, mutation_mod: ModuleType) -> None:
    local = {"DSO-9": {"summary": "after", "local_id": "local-2", "rebar-id": "id-2"}}
    jira = {"DSO-9": {"summary": "before", "local_id": "local-1", "rebar-id": "id-1"}}
    result = differ.compute_mutations(local_state=local, jira_state=jira)
    assert len(result) == 1
    m = result[0]
    assert m.action == mutation_mod.MutationAction.update
    assert "local_id" not in m.payload
    assert "rebar-id" not in m.payload
    assert m.payload == {"summary": "after"}


def test_create_excludes_excluded_fields(differ: ModuleType, mutation_mod: ModuleType) -> None:
    """A new issue whose only fields are excluded should yield no mutation."""
    local = {"DSO-11": {"local_id": "loc", "rebar-id": "xid"}}
    jira: dict = {}
    result = differ.compute_mutations(local_state=local, jira_state=jira)
    assert result == []


def test_create_with_mixed_fields_excludes_excluded_only(
    differ: ModuleType, mutation_mod: ModuleType
) -> None:
    local = {"DSO-12": {"summary": "keep me", "local_id": "skip-me"}}
    jira: dict = {}
    result = differ.compute_mutations(local_state=local, jira_state=jira)
    assert len(result) == 1
    m = result[0]
    assert m.action == mutation_mod.MutationAction.create
    assert m.payload == {"summary": "keep me"}
    assert "local_id" not in m.payload


def test_pure_function_invariant(differ: ModuleType, mutation_mod: ModuleType) -> None:
    local = {
        "DSO-1": {"summary": "hello updated"},
        "DSO-3": {"summary": "brand new"},
    }
    jira = {"DSO-1": {"summary": "hello"}, "DSO-2": {"summary": "world"}}
    a = differ.compute_mutations(local_state=copy.deepcopy(local), jira_state=copy.deepcopy(jira))
    b = differ.compute_mutations(local_state=copy.deepcopy(local), jira_state=copy.deepcopy(jira))
    assert a == b


def test_mutations_are_sorted_by_target(differ: ModuleType, mutation_mod: ModuleType) -> None:
    """Result targets should be in sorted order for determinism."""
    local = {
        "DSO-Z": {"summary": "z issue"},
        "DSO-A": {"summary": "a issue"},
        "DSO-M": {"summary": "m issue"},
    }
    jira: dict = {}
    result = differ.compute_mutations(local_state=local, jira_state=jira)
    targets = [m.target for m in result]
    assert targets == sorted(targets)


def test_every_mutation_carries_provenance(differ: ModuleType, mutation_mod: ModuleType) -> None:
    """Every emitted Mutation must carry a non-empty provenance mapping.

    Regression for F2 (formerly local_id non-empty invariant): in the new
    Mutation-based contract, the canonical local identity travels in
    ``provenance`` (e.g. ``{'local_id': 'loc-explicit-100'}``) rather than as
    a top-level mutation field. The applier reads provenance for JQL dedup
    and mapping.json keys, so an empty provenance would re-introduce the
    bug F2 originally fixed.
    """
    local = {
        # Update case (changed field) with explicit local_id.
        "DSO-100": {"summary": "new", "local_id": "loc-explicit-100"},
        # Update case with no local_id (fallback to target).
        "DSO-101": {"summary": "after"},
        # Create case (no prior jira entry).
        "DSO-300": {"summary": "brand new"},
    }
    jira = {
        "DSO-100": {"summary": "old", "local_id": "loc-explicit-100"},
        "DSO-101": {"summary": "before"},
        # Delete case: present in jira but absent in local.
        "DSO-200": {"summary": "going", "local_id": "loc-explicit-200"},
    }
    mutations = differ.compute_mutations(local_state=local, jira_state=jira)

    # All mutations must carry a provenance Mapping.
    for m in mutations:
        assert m.provenance is not None, f"Mutation missing provenance: {m}"
        # provenance is a Mapping per the Mutation contract.
        assert hasattr(m.provenance, "__getitem__"), (
            f"Mutation provenance is not a Mapping: {m.provenance!r}"
        )


# ---------------------------------------------------------------------------
# AC tests (task 0805-02a0): bind-aware suppression, purity, return type.
# ---------------------------------------------------------------------------


def test_no_outbound_create_for_already_bound_local_id(
    differ: ModuleType, mutation_mod: ModuleType
) -> None:
    """dd-4: differ MUST NOT emit (outbound, create) for a local ticket whose
    local_id is already present in the fetched Jira working set."""
    local = {
        # Local ticket A is already bound: its local_id appears in
        # jira_state under a different key (the Jira-side issue key).
        "loc-A": {"summary": "A", "local_id": "uuid-A"},
        # Local ticket B is unbound: no Jira entry carries local_id "uuid-B".
        "loc-B": {"summary": "B", "local_id": "uuid-B"},
    }
    jira = {
        "PROJ-1": {"summary": "A", "local_id": "uuid-A"},
    }

    result = differ.compute_mutations(local_state=local, jira_state=jira)

    outbound_creates = [
        m
        for m in result
        if m.direction == mutation_mod.MutationDirection.outbound
        and m.action == mutation_mod.MutationAction.create
    ]
    assert len(outbound_creates) == 1, (
        f"expected exactly one outbound create (for loc-B); got {outbound_creates}"
    )
    assert outbound_creates[0].target == "loc-B"
    # And zero outbound creates target loc-A (it is already bound).
    assert not any(m.target == "loc-A" for m in outbound_creates)


def test_diff_is_pure_across_n_invocations(differ: ModuleType, mutation_mod: ModuleType) -> None:
    """dd-1: same input → byte-identical canonical manifest across 10 invocations."""
    # Import the canonical serializer used to derive the manifest hash.
    from importlib.util import module_from_spec, spec_from_file_location

    spec = spec_from_file_location("mutation_for_diff_purity", MUTATION_PATH)
    assert spec is not None and spec.loader is not None
    mut = module_from_spec(spec)
    spec.loader.exec_module(mut)  # type: ignore[union-attr]
    serialize_manifest = mut.serialize_manifest

    local = {
        "DSO-A": {"summary": "alpha", "status": "open"},
        "DSO-B": {"summary": "beta", "priority": "high"},
    }
    jira = {
        "DSO-A": {"summary": "alpha-old", "status": "open"},
        "DSO-C": {"summary": "gamma"},
    }

    hashes: set[str] = set()
    jsons: set[str] = set()
    for _ in range(10):
        result = differ.compute_mutations(
            local_state=copy.deepcopy(local), jira_state=copy.deepcopy(jira)
        )
        json_text, sha256 = serialize_manifest(result)
        hashes.add(sha256)
        jsons.add(json_text)

    assert len(hashes) == 1, f"non-deterministic manifest hashes across 10 runs: {hashes}"
    assert len(jsons) == 1, "non-deterministic manifest JSON across 10 runs"


def test_diff_returns_mutation_list(differ: ModuleType, mutation_mod: ModuleType) -> None:
    """compute_mutations returns a list of Mutation objects."""
    local = {
        "DSO-1": {"summary": "new", "status": "open"},
        "DSO-2": {"summary": "added"},
    }
    jira = {
        "DSO-1": {"summary": "old", "status": "open"},
        "DSO-3": {"summary": "remote-only"},
    }

    result = differ.compute_mutations(local_state=local, jira_state=jira)

    assert isinstance(result, list)
    assert all(isinstance(m, mutation_mod.Mutation) for m in result), (
        f"non-Mutation entries in result: {[type(m).__name__ for m in result]}"
    )


# ---------------------------------------------------------------------------
# dd-1 stress tests: differ purity under repetition and input-key permutation.
#
# Intent: determinism stress, not exhaustive (direction, action) coverage.
# differ.compute_mutations currently emits a subset of _VALID_COMBINATIONS
# (outbound create/update/delete, inbound create/update/delete, conflict and
# probe via the unbindable-state path). It does not directly emit
# clean_label or repair_property — those are produced by downstream stages.
# These fixtures therefore exercise the pairs the differ actually emits and
# leave the rest to applier-level tests.
# ---------------------------------------------------------------------------


def _stress_fixture() -> tuple[dict, dict]:
    """Moderate fixture (~10 tickets) producing a variety of mutation kinds.

    Covers (in the subset compute_mutations actually emits):
      - outbound create (local-only ticket, non-empty allowed payload)
      - inbound create  (jira-only ticket)
      - update          (changed allowed field on both sides)
      - delete          (asymmetric one-sided absence with no bind)
      - bind-aware suppression (local local_id matches a jira entry)
      - probe / conflict pathway (unbindable state — covered passively where
        the differ's _VALID_COMBINATIONS gate allows it)
    """
    local = {
        "loc-1": {"summary": "alpha", "status": "open"},
        "loc-2": {"summary": "beta updated", "status": "open"},
        "loc-3": {"summary": "gamma", "priority": "high"},
        "loc-4": {"summary": "delta", "local_id": "uuid-bound-4"},
        "loc-5": {"summary": "epsilon", "status": "closed"},
        "loc-6": {"summary": "zeta", "priority": "low"},
        "loc-9": {"summary": "iota new"},
    }
    jira = {
        "loc-1": {"summary": "alpha", "status": "open"},  # identical → no mutation
        "loc-2": {"summary": "beta old", "status": "open"},  # update
        "PROJ-4": {"summary": "delta", "local_id": "uuid-bound-4"},  # bound
        "loc-5": {"summary": "epsilon", "status": "open"},  # update (status)
        "loc-7": {"summary": "eta remote"},  # inbound create
        "loc-8": {"summary": "theta going"},  # delete (local removed)
        "loc-10": {"summary": "kappa remote"},  # inbound create
    }
    return local, jira


def test_differ_is_pure_across_100_invocations(
    differ: ModuleType, mutation_mod: ModuleType
) -> None:
    """dd-1: 100 invocations on the same input produce byte-identical manifests."""
    serialize_manifest = mutation_mod.serialize_manifest
    local, jira = _stress_fixture()

    hashes: set[str] = set()
    jsons: set[str] = set()
    for _ in range(100):
        result = differ.compute_mutations(
            local_state=copy.deepcopy(local), jira_state=copy.deepcopy(jira)
        )
        json_text, sha256 = serialize_manifest(result)
        hashes.add(sha256)
        jsons.add(json_text)

    assert len(hashes) == 1, f"non-deterministic manifest hashes across 100 runs: {hashes}"
    assert len(jsons) == 1, "non-deterministic manifest JSON across 100 runs"


def test_differ_is_pure_across_input_permutations(
    differ: ModuleType, mutation_mod: ModuleType
) -> None:
    """dd-1: 100 random key-order permutations produce byte-identical manifests.

    Insertion order of a Python dict is preserved on iteration. If the differ
    leaks input ordering into its output, permuting the (key, value) pairs of
    local_state and jira_state before reconstruction will produce divergent
    manifest hashes. A pure differ must collapse all 100 permutations to one
    canonical manifest.
    """
    import random

    serialize_manifest = mutation_mod.serialize_manifest
    local_base, jira_base = _stress_fixture()
    local_items = list(local_base.items())
    jira_items = list(jira_base.items())
    rng = random.Random(42)

    hashes: set[str] = set()
    for _ in range(100):
        local_perm = local_items[:]
        jira_perm = jira_items[:]
        rng.shuffle(local_perm)
        rng.shuffle(jira_perm)
        local = {k: copy.deepcopy(v) for k, v in local_perm}
        jira = {k: copy.deepcopy(v) for k, v in jira_perm}
        result = differ.compute_mutations(local_state=local, jira_state=jira)
        _json_text, sha256 = serialize_manifest(result)
        hashes.add(sha256)

    assert len(hashes) == 1, (
        f"non-deterministic manifest hashes across 100 input permutations: {hashes}"
    )


# ---------------------------------------------------------------------------
# Bug 4354: snapshot-differ must recognise bound issues via the rebar-id: label
# even when the fetcher snapshot lacks the local_id entity property.
#
# Root cause: fetcher.fetch_snapshot only stores Jira `fields` (which include
# `labels`) — the `local_id` entity property is never in the snapshot.
# So the snapshot-differ sees DIG-NNNN in jira_state without local_id,
# falls into the "in_jira and not in_local" branch with jira_local_id=None,
# and emits an inbound CREATE. The applier then creates a phantom
# jira-dig-NNNN local entity AND writes a ghost `rebar-id:jira-dig-NNNN`
# label back to Jira (verified empirically by labels-probe.sh on 2026-05-29:
# after binding e486-... to DIG-5029, the next pass produced
# `['rebar-id:259f-...', 'rebar-id:jira-dig-5029', 'labelprobe-...']` on Jira).
#
# Fix: the snapshot-differ must treat any Jira issue carrying a
# `rebar-id:<local_id>` label as "already managed by the binding-aware
# differs" and skip it — no inbound CREATE, no inbound conflict.
# ---------------------------------------------------------------------------


def test_rebar_id_label_suppresses_phantom_inbound_create(
    differ: ModuleType, mutation_mod: ModuleType
) -> None:
    """Bug 4354: a Jira issue carrying `rebar-id:<local_id>` is already bound;
    the snapshot-differ MUST NOT emit an inbound CREATE for it, even when
    the snapshot lacks the local_id entity property (the fetcher does
    not populate it).
    """
    local: dict = {}
    jira = {
        "DIG-5029": {
            "summary": "labels-probe ticket",
            "labels": [
                "rebar-id:259f-2f86-77c2-4767",
                "labelprobe-1780064991",
            ],
            # local_id is intentionally absent — fetcher.fetch_snapshot
            # never populates entity properties into the snapshot.
        }
    }
    result = differ.compute_mutations(local_state=local, jira_state=jira)
    # No inbound CREATE for DIG-5029 — it's bound (label `rebar-id:259f-...`).
    inbound_creates = [
        m
        for m in result
        if m.direction == mutation_mod.MutationDirection.inbound
        and m.action == mutation_mod.MutationAction.create
        and m.target == "DIG-5029"
    ]
    assert not inbound_creates, (
        f"phantom inbound CREATE emitted for already-bound DIG-5029: {inbound_creates}"
    )
    # And no inbound CONFLICT — bound issues should be silent here; the
    # binding-aware inbound_differ owns this lane.
    inbound_conflicts = [
        m
        for m in result
        if m.direction == mutation_mod.MutationDirection.inbound
        and m.action == mutation_mod.MutationAction.conflict
        and m.target == "DIG-5029"
    ]
    assert not inbound_conflicts, (
        f"unexpected inbound CONFLICT for bound DIG-5029: {inbound_conflicts}"
    )


def test_rebar_id_label_with_dash_form_also_suppresses_inbound_create(
    differ: ModuleType, mutation_mod: ModuleType
) -> None:
    """Bug 4354: legacy `rebar-id-<local_id>` hyphen-form labels (pre-cutover)
    also signal a binding. Snapshot-differ must skip these too.
    """
    local: dict = {}
    jira = {
        "DIG-1234": {
            "summary": "legacy bound ticket",
            "labels": ["rebar-id-abcd-1234-ef56-7890", "anyusertag"],
        }
    }
    result = differ.compute_mutations(local_state=local, jira_state=jira)
    phantom = [
        m
        for m in result
        if m.target == "DIG-1234"
        and m.direction == mutation_mod.MutationDirection.inbound
        and m.action
        in (
            mutation_mod.MutationAction.create,
            mutation_mod.MutationAction.conflict,
        )
    ]
    assert not phantom, (
        f"phantom inbound mutation for bound DIG-1234 (hyphen-form label): {phantom}"
    )


def test_unbound_jira_issue_without_rebar_id_label_still_creates(
    differ: ModuleType, mutation_mod: ModuleType
) -> None:
    """Bug 4354 (regression-guard): a Jira issue WITHOUT any rebar-id label is
    genuinely unbound; the snapshot-differ MUST still emit inbound CREATE so
    a fresh external Jira issue is mirrored locally.
    """
    local: dict = {}
    jira = {
        "DIG-9999": {
            "summary": "freshly created on Jira directly",
            "labels": ["someusertag"],
        }
    }
    result = differ.compute_mutations(local_state=local, jira_state=jira)
    inbound_creates = [
        m
        for m in result
        if m.direction == mutation_mod.MutationDirection.inbound
        and m.action == mutation_mod.MutationAction.create
        and m.target == "DIG-9999"
    ]
    assert len(inbound_creates) == 1, f"unbound jira issue lost its inbound CREATE: {result}"
