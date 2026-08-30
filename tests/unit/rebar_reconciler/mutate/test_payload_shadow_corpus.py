"""Portable legacy-vs-typed shadow replay over the versioned corpus (ADR 0107, e9d5).

Loads ``tests/fixtures/reconciler/payload_corpus/v1/scenarios.json`` and drives
every scenario through ``rebar_reconciler.payload_shadow``:

* ``expect: "match"`` scenarios must produce byte-identical legacy/typed
  ``serialize_manifest`` output (AC5: "zero unexplained differences").
* ``expect: "reject"`` scenarios must raise from typed construction — each is
  an approved, named, rationale-carrying intentional delta (see the corpus
  README's "Approved intentional deltas" section and the ticket comment
  recording sign-off).

No I/O anywhere in this file: pure JSON load + pure dataclass construction +
pure serialization compare.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rebar_reconciler import mutation as mutation_mod
from rebar_reconciler import mutation_payloads, payload_shadow

CORPUS_PATH = (
    Path(__file__).resolve().parents[3]
    / "fixtures"
    / "reconciler"
    / "payload_corpus"
    / "v1"
    / "scenarios.json"
)


def _load_corpus() -> list[dict]:
    assert CORPUS_PATH.exists(), f"corpus missing at {CORPUS_PATH}"
    return json.loads(CORPUS_PATH.read_text())


CORPUS = _load_corpus()
MATCH_SCENARIOS = [s for s in CORPUS if s.get("expect", "match") == "match"]
REJECT_SCENARIOS = [s for s in CORPUS if s.get("expect") == "reject"]


def test_corpus_covers_every_live_combination_and_required_categories():
    """AC3: the corpus covers create/update/delete/probe/conflict, inbound-only
    actions, links/comments/labels/status/parent/assignee/binding fields,
    duplicate/malformed inputs, suppression/follow-ons, ambiguous outcomes,
    retry exhaustion, and multi-project routing."""
    combos = {(s["direction"], s["action"]) for s in CORPUS}
    expected_live_combos = {
        ("outbound", "create"),
        ("outbound", "update"),
        ("outbound", "delete"),
        ("outbound", "probe"),
        ("outbound", "conflict"),
        ("inbound", "create"),
        ("inbound", "update"),
        ("inbound", "clean_label"),
        ("inbound", "repair_property"),
        ("inbound", "conflict"),
    }
    assert expected_live_combos <= combos
    categories = {s["category"] for s in CORPUS}
    assert categories == {
        "happy_path",
        "edge_malformed",
        "edge_duplicate",
        "suppression_follow_on",
        "ambiguous_outcome",
        "retry_exhaustion",
        "multi_project_routing",
        "dead_by_design",
    }
    # links/comments/labels/status/parent/assignee/binding-field coverage:
    flat_payload_text = json.dumps([s["payload"] for s in CORPUS])
    for needle in (
        "labels",
        "comments",
        "links",
        "status",
        "parent",
        "assignee",
        "_bridge_target_project",
    ):
        assert needle in flat_payload_text, f"corpus never exercises {needle!r}"


@pytest.mark.parametrize("scenario", MATCH_SCENARIOS, ids=[s["id"] for s in MATCH_SCENARIOS])
def test_match_scenarios_have_zero_unexplained_differences(scenario):
    result = payload_shadow.compare_scenario(mutation_mod, scenario)
    assert result.matched, result.diff_summary


@pytest.mark.parametrize("scenario", REJECT_SCENARIOS, ids=[s["id"] for s in REJECT_SCENARIOS])
def test_reject_scenarios_have_a_rationale_and_typed_construction_raises(scenario):
    assert scenario.get("rationale"), f"{scenario['id']}: reject scenario missing rationale"
    with pytest.raises((ValueError, TypeError, mutation_payloads.UnknownMutationKindError)):
        payload_shadow.build_typed_mutation(
            mutation_mod,
            direction=scenario["direction"],
            action=scenario["action"],
            target=scenario["target"],
            payload=scenario["payload"],
            provenance=scenario.get("provenance", {}),
        )
    # The legacy twin must still construct fine (that's WHY it's a delta —
    # legacy accepted it, typed rejects it).
    payload_shadow.build_legacy_mutation(
        mutation_mod,
        direction=scenario["direction"],
        action=scenario["action"],
        target=scenario["target"],
        payload=scenario["payload"],
        provenance=scenario.get("provenance", {}),
    )


def test_replay_is_deterministic_and_idempotent():
    """Running the whole match-corpus twice must yield identical results both
    times — no clock, no random, no I/O side channel."""
    first = payload_shadow.compare_corpus(mutation_mod, MATCH_SCENARIOS)
    second = payload_shadow.compare_corpus(mutation_mod, MATCH_SCENARIOS)
    assert first.keys() == second.keys()
    for scenario_id in first:
        assert first[scenario_id] == second[scenario_id]
        assert first[scenario_id].matched


def test_duplicate_scenario_pair_is_idempotent():
    """edge_duplicate: two scenarios with identical (direction, action,
    target, payload) — replaying them must produce identical comparison
    results (idempotent), never diverging results for identical inputs."""
    dup = [s for s in CORPUS if s["category"] == "edge_duplicate"]
    assert len(dup) >= 2
    results = [payload_shadow.compare_scenario(mutation_mod, s) for s in dup]
    assert all(r.matched for r in results)
    assert len({r.legacy_hash for r in results}) == 1
    assert len({r.typed_hash for r in results}) == 1


def test_every_scenario_id_is_unique():
    ids = [s["id"] for s in CORPUS]
    assert len(ids) == len(set(ids))
