"""Regression tests for bug 1788-6149-e788-463f.

`applier.apply(list[Mutation], pass_id, repo_root)` previously crashed with
'Mutation' object has no attribute 'get' when fed Mutation dataclass
instances — the polymorphic dispatch fell through to `_apply_batch` which
calls `.get()` on each element.

These tests stub `_apply_batch` and assert observable behavior:
  1. list-of-Mutation reaches _apply_batch as list-of-DICT (no Mutation
     instances leak through).
  2. Each dict has the keys _apply_batch expects (action / fields / key /
     local_id / follow_on / direction) and ONLY JSON-serializable values.
  3. Empty payload.fields preserves the empty dict (does not truthy-fall
     through to the whole payload).
  4. Inbound typed Mutations raise TypeError rather than silently routing
     through outbound batch handlers (fail-closed guard).
"""

import importlib.util
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
APPLIER_PATH = (
    REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "applier.py"
)
MUTATION_PATH = (
    REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "mutation.py"
)

# Narrow set of sys.modules keys this test owns. Other tests' modules are
# not evicted so they don't suffer cross-test interference.
_OWNED_KEYS = (
    "rebar_reconciler.applier",
    "rebar_reconciler.mutation",
)


def _load(name: str, path: Path):
    if name in sys.modules:
        return sys.modules[name]
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def applier_and_mutation():
    """Load applier + mutation under canonical keys. Clean up only the
    specific keys this test installs (NOT any module containing "applier"
    or "mutation" in its name — that was too broad and risked cross-test
    interference).
    """
    saved = {k: sys.modules.pop(k, None) for k in _OWNED_KEYS}
    try:
        mut_mod = _load(_OWNED_KEYS[1], MUTATION_PATH)
        applier = _load(_OWNED_KEYS[0], APPLIER_PATH)
        yield applier, mut_mod
    finally:
        for k in _OWNED_KEYS:
            sys.modules.pop(k, None)
        for k, v in saved.items():
            if v is not None:
                sys.modules[k] = v


def _make_outbound_create(mut_mod, target="DIG-100", payload=None):
    return mut_mod.Mutation(
        direction=mut_mod.MutationDirection.outbound,
        action=mut_mod.MutationAction.create,
        target=target,
        payload=payload or {"fields": {"summary": "test"}, "local_id": "local-abc"},
        provenance={"source": "test"},
    )


def test_list_of_mutation_normalized_to_dict_before_apply_batch(
    applier_and_mutation, tmp_path
):
    """applier.apply([Mutation], ...) must pass list-of-dict (not list-of-
    Mutation) to _apply_batch. Assert observable behavior: _apply_batch
    is called with dicts having the expected keys; no Mutation leaks.
    """
    applier, mut_mod = applier_and_mutation
    mutations = [_make_outbound_create(mut_mod)]

    with patch.object(applier, "_apply_batch", return_value=tmp_path / "m.json") as ab:
        applier.apply(mutations, "pass-1", repo_root=tmp_path)

    assert ab.call_count == 1
    passed_list = ab.call_args[0][0]
    assert isinstance(passed_list, list)
    assert all(isinstance(m, dict) for m in passed_list), (
        f"Mutation leaked through: {[type(m).__name__ for m in passed_list]}"
    )
    assert all("action" in m and "key" in m for m in passed_list), (
        "normalized dicts missing required batch keys"
    )


def test_normalized_dict_is_json_serializable(applier_and_mutation, tmp_path):
    """Every value in the normalized dict must be JSON-serializable —
    _apply_batch later writes the manifest via json.dumps. A non-
    serializable value (e.g. a Mutation back-reference) would crash there.
    """
    applier, mut_mod = applier_and_mutation
    mutations = [_make_outbound_create(mut_mod)]

    with patch.object(applier, "_apply_batch", return_value=tmp_path / "m.json") as ab:
        applier.apply(mutations, "pass-2", repo_root=tmp_path)

    passed_list = ab.call_args[0][0]
    for m in passed_list:
        try:
            json.dumps(m)
        except TypeError as exc:
            pytest.fail(
                f"normalized dict is not JSON-serializable ({exc}); "
                f"a non-serializable value (e.g. Mutation back-ref) would "
                f"crash _apply_batch's manifest write"
            )


def test_empty_fields_does_not_fall_through_to_full_payload(
    applier_and_mutation, tmp_path
):
    """payload.get('fields', payload) — NOT `or payload`. An intentionally
    empty fields dict must reach _apply_batch as {} (not as the full
    payload), to prevent leaking local_id / follow_on into batch fields.
    """
    applier, mut_mod = applier_and_mutation
    mutations = [
        _make_outbound_create(
            mut_mod,
            payload={"fields": {}, "local_id": "L1", "follow_on": {"kind": "x"}},
        ),
    ]

    with patch.object(applier, "_apply_batch", return_value=tmp_path / "m.json") as ab:
        applier.apply(mutations, "pass-3", repo_root=tmp_path)

    passed_list = ab.call_args[0][0]
    assert passed_list[0]["fields"] == {}, (
        f"empty fields fell through to full payload: {passed_list[0]['fields']!r}"
    )
    # local_id / follow_on still preserved as top-level keys
    assert passed_list[0]["local_id"] == "L1"
    assert passed_list[0]["follow_on"] == {"kind": "x"}


def _make_inbound_create(mut_mod, target="local-abc"):
    return mut_mod.Mutation(
        direction=mut_mod.MutationDirection.inbound,
        action=mut_mod.MutationAction.create,
        target=target,
        payload={"fields": {"summary": "in"}, "local_id": target},
        provenance={"source": "test"},
    )


def test_inbound_mutations_dispatched_per_mutation_via_apply_typed(
    applier_and_mutation, tmp_path
):
    """`apply(list[Mutation], pass_id, repo_root)` with inbound Mutations
    routes each one through `_apply_typed` (per-mutation dispatch via the
    _LEAVES registry) rather than the outbound batch path. Defect #8 — the
    previous fail-closed guard correctly identified this gap; this test pins
    the actual routing.
    """
    applier, mut_mod = applier_and_mutation
    inbound = [_make_inbound_create(mut_mod, target=f"local-{i}") for i in range(3)]

    with (
        patch.object(applier, "_apply_typed") as at,
        patch.object(applier, "_apply_batch", return_value=tmp_path / "m.json") as ab,
    ):
        applier.apply(inbound, "pass-inbound", repo_root=tmp_path)

    # Each inbound Mutation reached _apply_typed exactly once, in order.
    assert at.call_count == len(inbound), (
        f"Expected {len(inbound)} per-mutation _apply_typed dispatches; "
        f"got {at.call_count}"
    )
    dispatched_targets = [c.args[0].target for c in at.call_args_list]
    assert dispatched_targets == [m.target for m in inbound]
    # No inbound mutation leaked into the outbound batch path — _apply_batch
    # was called with an empty list (or not called at all if the impl skips
    # the empty-batch invocation; both are acceptable as long as no inbound
    # Mutation reaches it).
    if ab.call_count > 0:
        passed = ab.call_args[0][0]
        assert all(
            not (
                hasattr(m, "direction")
                and str(getattr(m.direction, "value", m.direction)) == "inbound"
            )
            for m in passed
        ), f"Inbound Mutation leaked into _apply_batch: {passed!r}"


def test_mixed_inbound_and_outbound_partitioned_correctly(
    applier_and_mutation, tmp_path
):
    """When `mutations` mixes inbound + outbound, the inbound ones go
    through `_apply_typed` (per-mutation) and the outbound ones go through
    `_apply_batch` (normalized to dicts). No mutation crosses paths."""
    applier, mut_mod = applier_and_mutation
    mutations = [
        _make_outbound_create(mut_mod, target="DIG-1"),
        _make_inbound_create(mut_mod, target="local-1"),
        _make_outbound_create(mut_mod, target="DIG-2"),
        _make_inbound_create(mut_mod, target="local-2"),
    ]

    with (
        patch.object(applier, "_apply_typed") as at,
        patch.object(applier, "_apply_batch", return_value=tmp_path / "m.json") as ab,
    ):
        applier.apply(mutations, "pass-mixed", repo_root=tmp_path)

    # Inbound: per-mutation dispatch via _apply_typed (2 of them).
    assert at.call_count == 2
    dispatched_inbound = [c.args[0].target for c in at.call_args_list]
    assert set(dispatched_inbound) == {"local-1", "local-2"}

    # Outbound: batch dispatch via _apply_batch (1 call with 2 dicts).
    assert ab.call_count == 1
    batch_arg = ab.call_args[0][0]
    assert len(batch_arg) == 2, (
        f"Expected 2 outbound dicts; got {len(batch_arg)}: {batch_arg!r}"
    )
    assert all(isinstance(m, dict) for m in batch_arg), (
        f"Outbound items were not normalized to dicts: "
        f"{[type(m).__name__ for m in batch_arg]}"
    )
    batch_keys = {m["key"] for m in batch_arg}
    assert batch_keys == {"DIG-1", "DIG-2"}


def test_all_inbound_does_not_crash_when_outbound_batch_is_empty(
    applier_and_mutation, tmp_path
):
    """When `mutations` is entirely inbound, the outbound batch path
    receives an empty list (or is skipped). Either way, `apply()` does
    not raise — defect #8 production scenario (empty local mirror,
    every Jira issue is an inbound 'create-locally')."""
    applier, mut_mod = applier_and_mutation
    inbound = [_make_inbound_create(mut_mod, target=f"local-{i}") for i in range(5)]

    with (
        patch.object(applier, "_apply_typed") as at,
        patch.object(applier, "_apply_batch", return_value=tmp_path / "m.json"),
    ):
        # Should NOT raise.
        applier.apply(inbound, "pass-all-inbound", repo_root=tmp_path)

    assert at.call_count == 5
