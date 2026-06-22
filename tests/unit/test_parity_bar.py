"""The parity bar (7d58) that gates the LangGraph -> Pydantic AI cutover. The metric
computation is pure, so the GATE is exercised offline with synthetic paired records —
each gating criterion must fail the bar in isolation, and a clean non-inferior run must
pass.
"""

from __future__ import annotations

from rebar.llm.parity import ItemRecord, parallel_run_and_diff, parity_report


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
