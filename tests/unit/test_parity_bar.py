"""The parity bar (7d58) that gates the LangGraph -> Pydantic AI cutover. The metric
computation is pure, so the GATE is exercised offline with synthetic paired records —
each gating criterion must fail the bar in isolation, and a clean non-inferior run must
pass.
"""

from __future__ import annotations

from rebar.llm.parity import (
    ATTRIBUTION_ACCURACY_FLOOR,
    CONTAINER_MIN_GOLD,
    ItemRecord,
    attribution_accuracy,
    container_fidelity_report,
    parallel_run_and_diff,
    parity_report,
)


def _rec(valid=True, decision="advisory", errored=False, label=None):
    return ItemRecord(valid=valid, decision=decision, errored=errored, label=label)


def _good_pair(n=100, *, block_gold=10, safe_gold=10):
    """Two identical, all-valid runs over n items, with a gold subset perfectly handled."""
    v1, v2 = [], []
    for i in range(n):
        label = "block" if i < block_gold else ("advisory" if i < block_gold + safe_gold else None)
        decision = "block" if label == "block" else "advisory"
        v1.append(_rec(decision=decision, label=label))
        v2.append(_rec(decision=decision, label=label))
    return v1, v2


def test_non_inferior_run_passes():
    v1, v2 = _good_pair()
    report = parity_report(v1, v2)
    assert report.passed, report.gating_failures
    assert report.metrics["validity"] == {"v1": 1.0, "v2": 1.0}
    assert report.metrics["decision_flips"] == 0


def test_validity_regression_fails():
    v1, v2 = _good_pair()
    v2[0] = _rec(valid=False, decision="advisory")  # one parse failure on v2
    report = parity_report(v1, v2)
    assert not report.passed
    assert any("validity" in f for f in report.gating_failures)


def test_agreement_below_floor_fails():
    # 20 gold (so the coverage guard is satisfied) + non-gold disagreement so ONLY the
    # 95% agreement floor trips: v2 flips 10 of the 100 non-gold items.
    v1, v2 = _good_pair(n=120, block_gold=10, safe_gold=10)
    for i in range(20, 30):  # 10/120 ~ 8.3% disagree -> agreement < 95%
        v2[i] = _rec(decision="dropped")
    report = parity_report(v1, v2)
    assert not report.passed
    assert any("agreement" in f for f in report.gating_failures)
    assert not any("gold set too small" in f for f in report.gating_failures)


def test_decision_flip_on_gold_fails():
    v1, v2 = _good_pair()
    v2[0] = _rec(decision="advisory", label="block")  # a should-block item flips to advisory
    report = parity_report(v1, v2)
    assert not report.passed
    assert any("flip" in f for f in report.gating_failures)


def test_recall_drop_beyond_margin_fails():
    # 30 gold (20 block + 10 safe) so the coverage guard passes; v2 misses 3 block
    # items -> recall drops 0.15 >> 0.02 margin (and they are gold flips).
    v1, v2 = _good_pair(n=100, block_gold=20, safe_gold=10)
    for i in range(3):
        v2[i] = _rec(decision="dropped", label="block")
    report = parity_report(v1, v2)
    assert not report.passed
    assert any("recall" in f or "flip" in f for f in report.gating_failures)


def test_error_rate_regression_fails():
    v1, v2 = _good_pair()  # 20 gold -> coverage guard satisfied
    v2[50] = _rec(errored=True)  # a non-gold item errors on v2
    report = parity_report(v1, v2)
    assert not report.passed
    assert any("error rate" in f for f in report.gating_failures)


def test_goldless_run_fails_coverage_guard():
    # The review's blind spot: a 4% block->dropped regression on a GOLDLESS corpus
    # keeps agreement at 96% (> floor) and would otherwise "pass" — the coverage guard
    # makes it FAIL instead, since recall/false-accept cannot be certified.
    v1, v2 = _good_pair(n=100, block_gold=0, safe_gold=0)
    v1 = [_rec(decision="block") for _ in v1]
    v2 = [_rec(decision="block") for _ in v2]
    for i in range(4):
        v2[i] = _rec(decision="dropped")  # a real "stops catching problems" regression
    report = parity_report(v1, v2)
    assert not report.passed
    assert any("gold set too small" in f for f in report.gating_failures)
    assert report.metrics["n_gold"] == 0


def test_parallel_run_and_diff_drives_both_runners():
    # Items 0-19 are gold (safe), handled identically -> passes the coverage guard.
    corpus = [{"id": i, "label": "advisory" if i < 20 else None} for i in range(40)]

    def run(_item):
        return {"decision": "advisory", "valid": True}

    def to_record(item, result, errored):
        if errored or result is None:
            return ItemRecord(valid=False, decision="dropped", errored=True, label=item["label"])
        return ItemRecord(valid=result["valid"], decision=result["decision"], label=item["label"])

    report = parallel_run_and_diff(corpus, run, run, to_record=to_record)
    assert report.passed, report.gating_failures
    assert report.metrics["n"] == 40 and report.metrics["n_gold"] == 20


def test_mismatched_lengths_raise():
    import pytest

    with pytest.raises(ValueError, match="align"):
        parity_report([_rec()], [])  # paired records must be the same length


def test_min_gold_override_relaxes_the_coverage_guard():
    # The coverage guard is tunable: with min_gold=0 a goldless run is no longer failed
    # ON THAT CRITERION (other criteria still apply).
    v1, v2 = _good_pair(n=10, block_gold=0, safe_gold=0)
    report = parity_report(v1, v2, min_gold=0)
    assert report.passed, report.gating_failures
    assert report.metrics["n_gold"] == 0


# ── container fidelity (G3/G4) — the S4/S5 candidate-vs-baseline gate (da34) ─────


def _container_rec(crit, pred, *, decision="block"):
    """A container gold record: gold criterion `crit`, runner attributed `pred`, with the
    gold label so parity's recall/false-accept counts it."""
    return ItemRecord(
        valid=True,
        decision=decision,
        label="block" if decision == "block" else "advisory",
        gold_criterion=crit,
        pred_criterion=pred,
    )


def _container_corpus(n_each=8):
    """A balanced G3/G4 gold corpus (>= CONTAINER_MIN_GOLD), each finding caught and
    attributed correctly — the baseline a faithful candidate must match."""
    recs = []
    for _ in range(n_each):
        recs.append(_container_rec("G3", "G3"))
        recs.append(_container_rec("G4", "G4"))
    return recs


def test_attribution_accuracy_perfect_and_partial():
    recs = _container_corpus(4)  # 8 caught, all attributed right
    assert attribution_accuracy(recs) == 1.0
    recs[0] = _container_rec("G3", "G4")  # one coverage gap mis-routed to G4
    assert attribution_accuracy(recs) == 7 / 8
    # A MISSED finding (not blocked) is a recall miss, not an attribution error.
    assert attribution_accuracy([_container_rec("G3", "G3", decision="dropped")]) == 1.0


def test_container_fidelity_faithful_candidate_passes():
    baseline = _container_corpus()
    candidate = _container_corpus()  # identical, faithful merged/packed path
    report = container_fidelity_report(baseline, candidate)
    assert report.passed, report.gating_failures
    assert report.metrics["attribution_accuracy"] == {"baseline": 1.0, "candidate": 1.0}
    assert report.metrics["n_gold"] >= CONTAINER_MIN_GOLD


def test_container_fidelity_misattribution_fails():
    baseline = _container_corpus()
    candidate = _container_corpus()
    # The merged candidate routes 3 of the G3 coverage gaps to G4 — recall is intact
    # (still blocked) but attribution drops below the floor.
    for i in range(0, 6, 2):
        candidate[i] = _container_rec("G3", "G4")
    report = container_fidelity_report(baseline, candidate)
    assert not report.passed
    assert any("attribution" in f for f in report.gating_failures)
    assert report.metrics["attribution_accuracy"]["candidate"] < ATTRIBUTION_ACCURACY_FLOOR


def test_container_fidelity_recall_drop_fails():
    baseline = _container_corpus()
    candidate = _container_corpus()
    # The packed candidate DROPS 3 findings (block -> dropped) — a recall regression the
    # reused parity bar (not the attribution layer) catches.
    for i in range(3):
        candidate[i] = ItemRecord(
            valid=True, decision="dropped", label="block", gold_criterion="G3", pred_criterion=None
        )
    report = container_fidelity_report(baseline, candidate)
    assert not report.passed
    assert any("recall" in f or "flip" in f for f in report.gating_failures)


def test_container_fidelity_min_gold_floor_enforced():
    # Too few labelled G3/G4 items -> recall/false-accept cannot be certified -> FAIL,
    # reusing parity's min_gold guard at the container-specific floor.
    baseline = [_container_rec("G3", "G3"), _container_rec("G4", "G4")]
    candidate = [_container_rec("G3", "G3"), _container_rec("G4", "G4")]
    report = container_fidelity_report(baseline, candidate)
    assert not report.passed
    assert any("gold set too small" in f for f in report.gating_failures)


def test_parallel_run_and_diff_maps_a_raising_runner_to_errored():
    # A runner that raises on an item must record an `errored` ItemRecord (not crash the
    # driver), so a v2 error regression is visible to the gate.
    corpus = [{"id": i, "label": "advisory" if i < 20 else None} for i in range(20)]

    def ok(_item):
        return {"decision": "advisory", "valid": True}

    def boom(_item):
        raise RuntimeError("runner down")

    def to_record(item, result, errored):
        if errored or result is None:
            return ItemRecord(valid=False, decision="dropped", errored=True, label=item["label"])
        return ItemRecord(valid=result["valid"], decision=result["decision"], label=item["label"])

    report = parallel_run_and_diff(corpus, ok, boom, to_record=to_record)  # v2 raises
    assert not report.passed
    assert any("error rate" in f for f in report.gating_failures)
    assert report.metrics["error_rate"]["v2"] == 1.0 and report.metrics["error_rate"]["v1"] == 0.0
