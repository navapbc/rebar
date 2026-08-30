"""Tests for the Tier-1 Pass-2 replay harness (``rebar.llm.evals.plan_replay.tier1``,
ticket presolar-finable-binturong / 53ab-bdf6-de1c-4bb1).

Pure computation (sampling, per-question agreement, distribution shift) is tested
directly against plain dicts. The live ``build_candidate_runner``/``run_tier1`` seam is
exercised with a monkeypatched runner, never a real Bedrock call -- the live
commissioning run is a separate, deliberately gated manual step (see the ticket's
Acceptance Criteria).
"""

from __future__ import annotations

import pytest

from rebar.llm.evals.plan_replay import ledger, sampling, tier1
from rebar.llm.evals.plan_replay.verifier_candidates import (
    VerifierCandidate,
    load_verifier_candidate,
)

pytestmark = pytest.mark.unit


# ── sampling.py ──────────────────────────────────────────────────────────────────
def _pool_row(ticket_id: str, uuid_suffix: str, **overrides) -> dict:
    row = {
        "ticket_id": ticket_id,
        "review_event_uuid": f"uuid-{uuid_suffix}",
        "verdict": "PASS",
        "children": [],
        "finding_count": 2,
        "store": "rebar",
        "impact_model_version": "plan-v5",
    }
    row.update(overrides)
    return row


def test_finding_count_bucket_boundaries():
    assert sampling.finding_count_bucket(0) == "0"
    assert sampling.finding_count_bucket(1) == "1-3"
    assert sampling.finding_count_bucket(3) == "1-3"
    assert sampling.finding_count_bucket(4) == "4-10"
    assert sampling.finding_count_bucket(10) == "4-10"
    assert sampling.finding_count_bucket(11) == "11+"


def test_stratified_sample_is_deterministic_for_a_seed():
    rows = [_pool_row(f"t{i}", str(i)) for i in range(20)]
    a = sampling.stratified_sample(rows, n=5, seed=42)
    b = sampling.stratified_sample(rows, n=5, seed=42)
    assert a == b
    assert len(a) == 5


def test_stratified_sample_different_seed_can_differ():
    rows = [_pool_row(f"t{i}", str(i)) for i in range(20)]
    a = sampling.stratified_sample(rows, n=5, seed=1)
    b = sampling.stratified_sample(rows, n=5, seed=2)
    assert a != b


def test_stratified_sample_respects_strata_round_robin():
    # Two strata (leaf vs container), 3 rows each; sampling 4 should draw from both.
    leaves = [_pool_row(f"leaf{i}", f"l{i}") for i in range(3)]
    containers = [
        _pool_row(f"container{i}", f"c{i}", children=[{"ticket_id": "child"}]) for i in range(3)
    ]
    sample = sampling.stratified_sample(leaves + containers, n=4, seed=0)
    kinds = {"container" if r["children"] else "leaf" for r in sample}
    assert kinds == {"leaf", "container"}


def test_stratified_sample_caps_at_pool_size():
    rows = [_pool_row("t0", "0")]
    assert len(sampling.stratified_sample(rows, n=5, seed=0)) == 1


def test_stratified_sample_empty_n_or_rows():
    assert sampling.stratified_sample([], n=5, seed=0) == []
    assert sampling.stratified_sample([_pool_row("t0", "0")], n=0, seed=0) == []


# ── verifier_candidates.py ───────────────────────────────────────────────────────
def test_load_verifier_candidate_none_is_reproduction_run():
    candidate = load_verifier_candidate(None)
    assert candidate == VerifierCandidate(prompt_path=None)


def test_load_verifier_candidate_existing_path(tmp_path):
    prompt_file = tmp_path / "custom.md"
    prompt_file.write_text("---\ncategory: review\n---\ncustom prompt body")
    candidate = load_verifier_candidate(str(prompt_file))
    assert candidate.prompt_path == str(prompt_file)


def test_load_verifier_candidate_missing_path_raises(tmp_path):
    with pytest.raises(FileNotFoundError):
        load_verifier_candidate(str(tmp_path / "nope.md"))


# ── per_question_agreement ───────────────────────────────────────────────────────
def _answer(**binary_overrides) -> dict:
    binary = {"is_verifiable": "yes", "evidence_entails_finding": "no"}
    binary.update(binary_overrides)
    return {"binary": binary, "severity_attributes": {"prod_impact": "high"}}


def test_per_question_agreement_perfect_match():
    replayed = [
        {
            "stored_answers": [_answer(), _answer()],
            "candidate_answers": [_answer(), _answer()],
        }
    ]
    result = tier1.per_question_agreement(replayed)
    assert result["binary"]["is_verifiable"]["raw_agreement"] == 1.0
    assert result["binary"]["is_verifiable"]["n"] == 2
    assert result["binary"]["is_verifiable"]["no_answer"] == 0
    assert result["severity_attributes"]["prod_impact"]["raw_agreement"] == 1.0


def test_per_question_agreement_disagreement():
    replayed = [
        {
            "stored_answers": [_answer(is_verifiable="yes")],
            "candidate_answers": [_answer(is_verifiable="no")],
        }
    ]
    result = tier1.per_question_agreement(replayed)
    assert result["binary"]["is_verifiable"]["raw_agreement"] == 0.0


def test_per_question_agreement_excludes_missing_candidate_answer_and_counts_it():
    replayed = [
        {
            "stored_answers": [_answer(), _answer()],
            "candidate_answers": [_answer(), None],
        }
    ]
    result = tier1.per_question_agreement(replayed)
    stats = result["binary"]["is_verifiable"]
    assert stats["n"] == 1  # only the answered finding counted
    assert stats["no_answer"] == 1  # the None-candidate finding excluded, not imputed


def test_per_question_agreement_kappa_none_with_fewer_than_two_pairs():
    replayed = [{"stored_answers": [_answer()], "candidate_answers": [_answer()]}]
    result = tier1.per_question_agreement(replayed)
    assert result["binary"]["is_verifiable"]["kappa"] is None


# ── distribution_shift ───────────────────────────────────────────────────────────
def _all_yes_binary() -> dict:
    return {
        "is_verifiable": "yes",
        "evidence_entails_finding": "yes",
        "path_reachable": "yes",
        "impact_follows_necessarily": "yes",
        "no_viable_alternative_explanation": "yes",
        "no_existing_mitigation": "yes",
        "severity_claim_justified": "yes",
    }


def _all_no_binary() -> dict:
    return {k: "no" for k in _all_yes_binary()}


def test_distribution_shift_validity_bucketing_known_ratio():
    # stored: validity=1.0 (all yes) -> bucket [0.9,1.0]; candidate: validity=0.0 (all no)
    # -> bucket [0.0,0.1). A full shift from one bucket to the other.
    replayed = [
        {
            "stored_answers": [{"binary": _all_yes_binary(), "severity_attributes": {}}],
            "candidate_answers": [{"binary": _all_no_binary(), "severity_attributes": {}}],
        }
    ]
    shift = tier1.distribution_shift(replayed, impact_fn=lambda attrs: 0.5)
    validity_shift = shift["validity"]
    assert validity_shift["total_variation_distance"] == 1.0
    assert validity_shift["count_delta"]["[0.9,1.0]"] == -1
    assert validity_shift["count_delta"]["[0.0,0.1)"] == 1


def test_distribution_shift_excludes_no_answer_findings():
    replayed = [
        {
            "stored_answers": [{"binary": _all_yes_binary(), "severity_attributes": {}}],
            "candidate_answers": [None],
        }
    ]
    shift = tier1.distribution_shift(replayed)
    # Nothing scored on either side -> TVD is 0 (empty distributions).
    assert shift["validity"]["total_variation_distance"] == 0.0


def test_distribution_shift_impact_uses_categorical_severity_attributes():
    replayed = [
        {
            "stored_answers": [
                {"binary": _all_yes_binary(), "severity_attributes": {"prod_impact": "high"}}
            ],
            "candidate_answers": [
                {"binary": _all_yes_binary(), "severity_attributes": {"prod_impact": "low"}}
            ],
        }
    ]
    shift = tier1.distribution_shift(replayed)
    prod_impact_shift = shift["impact"]["prod_impact"]
    assert prod_impact_shift["count_delta"] == {"high": -1, "low": 1}
    assert prod_impact_shift["total_variation_distance"] == 1.0


# ── ledger pre-flight ─────────────────────────────────────────────────────────────
def test_run_tier1_refuses_before_any_call_when_estimate_exceeds_budget(tmp_path, monkeypatch):
    ledger_path = str(tmp_path / "ledger.jsonl")
    # Burn the ledger to just under the cap so any tier1 estimate is refused.
    with open(ledger_path, "w", encoding="utf-8") as fh:
        import json as _json

        fh.write(_json.dumps({"usd": ledger.LEDGER_CAP_USD - ledger.LEDGER_RESERVE_USD}) + "\n")

    called = []
    monkeypatch.setattr(
        tier1,
        "build_sampling_pool",
        lambda *a, **kw: [
            {
                "ticket_id": "t0",
                "review_event_uuid": "u0",
                "verdict": "PASS",
                "children": [],
                "finding_count": 1,
                "store": "rebar",
                "impact_model_version": "plan-v5",
                "sidecar_data": {"findings": [], "description": ""},
            }
        ],
    )
    monkeypatch.setattr(
        tier1,
        "build_candidate_runner",
        lambda candidate: called.append("called") or (lambda *a: [], "bedrock:x", []),
    )
    monkeypatch.setattr(
        tier1.parity,
        "resolve_pinned_model",
        lambda pass_name: tier1.parity.PinnedModel(model_id="bedrock:x", config_root="/tmp"),
    )

    with pytest.raises(ledger.BudgetExceeded):
        tier1.run_tier1(
            {"rebar": "/tmp/fake"},
            cache_dir=str(tmp_path),
            candidate=VerifierCandidate(prompt_path=None),
            candidate_name="current",
            n=1,
            seed=0,
            ledger_path=ledger_path,
        )
    assert called == []  # build_candidate_runner never invoked -- refused before any call


# ── replay_review with a FakeRunner-style run_chunk ─────────────────────────────
def test_replay_review_no_answer_finding_from_omitted():
    def fake_run_chunk(instructions: str, context: str) -> list[dict]:
        return [{"index": 0, "binary": _all_yes_binary(), "severity_attributes": {}}]

    row = {
        "ticket_id": "t0",
        "review_event_uuid": "u0",
        "description": "plan text",
        "sidecar_data": {
            "findings": [
                {
                    "finding": "f0",
                    "criteria": [],
                    "evidence": [],
                    "impact": "x",
                    "verification": {"binary": _all_yes_binary(), "severity_attributes": {}},
                },
                # A finding so large it can never fit any chunk -> omitted, no answer.
                {
                    "finding": "f1 " + "x" * 2_000_000,
                    "criteria": [],
                    "evidence": [],
                    "impact": "x",
                    "verification": {"binary": _all_yes_binary(), "severity_attributes": {}},
                },
            ]
        },
    }
    result = tier1.replay_review(row, fake_run_chunk, "bedrock:x")
    assert result["candidate_answers"][0] is not None
    assert result["candidate_answers"][1] is None  # omitted -> no answer, not imputed
