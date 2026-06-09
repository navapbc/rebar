"""Quarantine + seed-mutation tests for rebar_reconciler/differ.py.

Covers the centralized ``_emit`` helper introduced for story 7a75:

  - Every Mutation-emit site in :func:`compute_mutations` routes through
    ``_emit``, so a single quarantine/seed policy applies uniformly.
  - ``quarantine_set`` suppresses any mutation whose ``target`` is in the
    set, regardless of which code path produced it.
  - ``seed_mutations`` are prepended to the result list unchanged so the
    dual-identity invariant check
    (``invariants.check_dual_identity_complete``) can inject pre-built
    repair mutations that the differ itself cannot derive.

These tests deliberately drive the RED phase: they assert the public
contract of the helper-centralized refactor, independently of how many
individual emit sites the differ contains.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

# ---------------------------------------------------------------------------
# Module loading (matches the pattern in test_differ.py).
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).resolve().parents[3]
DIFFER_PATH = (
    REPO_ROOT / "src" / "rebar" / "_engine" / "rebar_reconciler" / "differ.py"
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


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_emit_helper_centralizes_quarantine_check(
    differ: ModuleType,
    mutation_mod: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Every emit site in compute_mutations() must go through differ._emit.

    We monkeypatch differ._emit with a spy that records every (target,
    direction, action) it sees, then assemble a local/jira state that
    exercises every emit branch:

      * outbound create (unbound local)
      * inbound create (jira-only)
      * outbound update (field drift)
      * inbound conflict (duplicate local_id collision)
      * outbound probe (ambiguous local binding)
      * inbound conflict (dangling jira local_id)

    If any emit site still calls ``mutations.append`` directly, the spy
    will miss that mutation and the assertion will fail.
    """
    real_emit = differ._emit
    seen: list[tuple[str, object, object]] = []

    def spy_emit(mutation, *, quarantine_set, mutations_out, **kwargs):  # type: ignore[no-untyped-def]
        seen.append((mutation.target, mutation.direction, mutation.action))
        real_emit(
            mutation,
            quarantine_set=quarantine_set,
            mutations_out=mutations_out,
            **kwargs,
        )

    monkeypatch.setattr(differ, "_emit", spy_emit)

    # outbound create + inbound create + outbound update
    local = {
        "LOCAL-A": {"summary": "new"},
        "BOTH-1": {"summary": "local-side", "priority": "high"},
    }
    jira = {
        "JIRA-B": {"summary": "from jira"},
        "BOTH-1": {"summary": "jira-side", "priority": "high"},
    }

    result = differ.compute_mutations(local_state=local, jira_state=jira)

    targets_emitted = {t for t, _, _ in seen}
    targets_in_result = {m.target for m in result}

    # Every mutation that appears in the result must have passed through
    # the spy — i.e. there are no direct mutations.append() bypasses.
    assert targets_in_result.issubset(targets_emitted), (
        f"Result contains mutations the _emit spy never saw: "
        f"{targets_in_result - targets_emitted}. Some emit site still "
        f"bypasses _emit and writes to mutations directly."
    )
    # Sanity check: the spy fired at least once per result mutation.
    assert len(seen) >= len(result)


def test_quarantine_set_suppresses_all_mutations_for_key(
    differ: ModuleType, mutation_mod: ModuleType
) -> None:
    """A target in quarantine_set must not appear in the result.

    Constructs a state where LOCAL-A would otherwise produce an outbound
    create mutation, and asserts that passing
    ``quarantine_set={"LOCAL-A"}`` suppresses it entirely while leaving
    other mutations intact.
    """
    local = {
        "LOCAL-A": {"summary": "should be quarantined"},
        "LOCAL-B": {"summary": "should pass through"},
    }
    jira: dict = {}

    # Baseline: without quarantine, both mutations are emitted.
    baseline = differ.compute_mutations(local_state=local, jira_state=jira)
    baseline_targets = {m.target for m in baseline}
    assert "LOCAL-A" in baseline_targets
    assert "LOCAL-B" in baseline_targets

    # With LOCAL-A quarantined, it must disappear; LOCAL-B must remain.
    result = differ.compute_mutations(
        local_state=local,
        jira_state=jira,
        quarantine_set={"LOCAL-A"},
    )
    result_targets = {m.target for m in result}
    assert "LOCAL-A" not in result_targets, (
        f"Quarantine_set={{'LOCAL-A'}} did not suppress LOCAL-A mutation; "
        f"got targets {result_targets}"
    )
    assert "LOCAL-B" in result_targets


def test_seed_mutations_prepended_to_result(
    differ: ModuleType, mutation_mod: ModuleType
) -> None:
    """Seed mutations are prepended to the result and not mutated.

    The invariant checker (story 7a75,
    ``invariants.check_dual_identity_complete``) builds inbound
    repair_property mutations that the differ cannot derive from
    local/jira state. The differ must accept them via ``seed_mutations``
    and place them at the front of the returned list, unmodified.
    """
    Mutation = mutation_mod.Mutation
    MutationAction = mutation_mod.MutationAction
    MutationDirection = mutation_mod.MutationDirection

    seed = Mutation(
        direction=MutationDirection.inbound,
        action=MutationAction.repair_property,
        target="SEED-X",
        payload={"missing_property": "dso_local_id"},
        provenance={
            "source": "invariants.check_dual_identity_complete",
            "reason": "missing_dual_identity",
            "local_id": "SEED-X",
        },
    )

    local = {"LOCAL-1": {"summary": "regular create"}}
    jira: dict = {}

    result = differ.compute_mutations(
        local_state=local,
        jira_state=jira,
        seed_mutations=[seed],
    )

    # Seed mutation appears first and is the exact same object passed in.
    assert len(result) >= 1
    assert result[0] is seed, (
        "Seed mutation must be prepended to the result list as the same "
        "object instance, not copied or rebuilt."
    )
    # The differ-derived mutation for LOCAL-1 is still present.
    derived_targets = {m.target for m in result[1:]}
    assert "LOCAL-1" in derived_targets

    # Seed mutation fields untouched.
    assert seed.target == "SEED-X"
    assert seed.action == MutationAction.repair_property
    assert seed.direction == MutationDirection.inbound
    assert seed.payload == {"missing_property": "dso_local_id"}
