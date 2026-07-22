"""The parity bar that gates the LangGraph -> Pydantic AI cutover (7d58).

Parity is NON-INFERIORITY, not byte-identity: both runners are LLM-driven, so exact
output equality is incoherent. Before the LangGraph stack is dropped (story d6d1), the
new ``PydanticAIRunner`` must clear a DEFINED bar on the standing eval corpus (same
model + decoding params, N>=3 repeats), computed by :func:`parity_report` from paired
per-item records:

  (a) structured-output VALIDITY: v2 >= v1 (target >= 99% valid parses, no regression);
  (b) per-criterion verdict AGREEMENT >= 95% AND ZERO decision-level flips
      (block/advisory/dropped) on the gold set;
  (c) RECALL and FALSE-ACCEPT each within +/-2pp of v1 (non-inferiority margin);
  (d) runtime ERROR/timeout rate v2 <= v1;
  (e) cost/latency recorded (informational, NON-gating).

Pure + dependency-free: the metric computation here is unit-tested with synthetic
records; :func:`parallel_run_and_diff` is the live driver (runs BOTH runners over a
corpus and feeds their records in) — that part needs the corpus + funded model calls,
but the GATE logic is exercised offline so the bar itself can't silently drift.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from typing import Any, TypeVar

_T = TypeVar("_T")

# A decision is the load-bearing, coarse outcome a verdict resolves to; a FLIP between
# these on the gold set is never acceptable (it changes what ships).
_DECISIONS = ("block", "advisory", "dropped")

# Non-inferiority margins (percentage points / fractions).
VALIDITY_FLOOR = 0.99
AGREEMENT_FLOOR = 0.95
NONINFERIORITY_MARGIN = 0.02


@dataclass(frozen=True)
class ItemRecord:
    """One runner's outcome on one corpus item (averaged over the N repeats)."""

    valid: bool  # the structured output parsed + validated
    decision: str  # block | advisory | dropped (the resolved coarse outcome)
    errored: bool = False  # a runtime error / timeout (not a model verdict)
    label: str | None = None  # the gold decision for this item (if it is a gold item)
    cost: float = 0.0  # informational
    latency_s: float = 0.0  # informational
    # Container fidelity (G3/G4) attribution, consumed by `container_fidelity_report`:
    # the GOLD criterion this item's finding belongs to, and the one the runner
    # ATTRIBUTED it to. Both None on non-container records (ignored by the parity gate).
    gold_criterion: str | None = None  # "G3" | "G4" | None
    pred_criterion: str | None = None  # the criterion the runner attributed the finding to
    gold_prerequisite_id: str | None = None
    pred_prerequisite_id: str | None = None


@dataclass
class ParityReport:
    passed: bool
    gating_failures: list[str] = field(default_factory=list)
    metrics: dict[str, Any] = field(default_factory=dict)


def _rate(records: Sequence[_T], pred: Callable[[_T], bool]) -> float:
    return (sum(1 for r in records if pred(r)) / len(records)) if records else 0.0


def _recall_false_accept(records: Sequence[ItemRecord]) -> tuple[float, float]:
    """Recall = caught / should-have-blocked; false-accept = wrongly-shipped / safe,
    against the per-item gold ``label`` (only gold items count)."""
    gold = [r for r in records if r.label in _DECISIONS]
    should_block = [r for r in gold if r.label == "block"]
    safe = [r for r in gold if r.label != "block"]
    recall = (
        sum(1 for r in should_block if r.decision == "block") / len(should_block)
        if should_block
        else 1.0
    )
    false_accept = sum(1 for r in safe if r.decision != "block") / len(safe) if safe else 0.0
    return recall, false_accept


# A parity run with too few gold items can't certify recall/false-accept; below this
# the verdict-agreement floor is the ONLY guard on decision regressions, which is not
# enough to authorize the cutover — so the bar FAILS rather than silently "passes".
MIN_GOLD_ITEMS = 20


def parity_report(
    v1: Sequence[ItemRecord], v2: Sequence[ItemRecord], *, min_gold: int = MIN_GOLD_ITEMS
) -> ParityReport:
    """Compute the parity verdict from paired records (``v1[i]`` and ``v2[i]`` are the
    two runners' outcomes on the same corpus item). Returns a :class:`ParityReport`
    whose ``passed`` is True only if EVERY gating criterion (a)-(d) holds AND the gold
    set is large enough to certify recall/false-accept (``min_gold``)."""
    if len(v1) != len(v2):
        raise ValueError(f"paired records must align: {len(v1)} vs {len(v2)}")

    val1, val2 = _rate(v1, lambda r: r.valid), _rate(v2, lambda r: r.valid)
    err1, err2 = _rate(v1, lambda r: r.errored), _rate(v2, lambda r: r.errored)
    pairs = list(zip(v1, v2, strict=True))  # lengths already validated above
    agreement = _rate(pairs, lambda p: p[0].decision == p[1].decision)
    gold_pairs = [(a, b) for a, b in pairs if a.label in _DECISIONS]
    n_gold = len(gold_pairs)
    decision_flips = sum(1 for a, b in gold_pairs if a.decision != b.decision)
    rec1, fa1 = _recall_false_accept(v1)
    rec2, fa2 = _recall_false_accept(v2)

    failures: list[str] = []
    # (a) validity: v2 must not regress, and clear the floor.
    if val2 + 1e-9 < val1:
        failures.append(f"validity regressed: v2 {val2:.3f} < v1 {val1:.3f}")
    if val2 + 1e-9 < VALIDITY_FLOOR:
        failures.append(f"validity {val2:.3f} below floor {VALIDITY_FLOOR}")
    # (b) agreement + zero decision flips on gold.
    if agreement + 1e-9 < AGREEMENT_FLOOR:
        failures.append(f"verdict agreement {agreement:.3f} below {AGREEMENT_FLOOR}")
    if decision_flips:
        failures.append(f"{decision_flips} decision-level flip(s) on the gold set (must be 0)")
    # (c) recall + false-accept non-inferiority.
    if (rec1 - rec2) > NONINFERIORITY_MARGIN + 1e-9:
        failures.append(f"recall dropped {rec1 - rec2:.3f} > margin {NONINFERIORITY_MARGIN}")
    if (fa2 - fa1) > NONINFERIORITY_MARGIN + 1e-9:
        failures.append(f"false-accept rose {fa2 - fa1:.3f} > margin {NONINFERIORITY_MARGIN}")
    # (d) runtime error rate must not regress.
    if err2 > err1 + 1e-9:
        failures.append(f"error rate regressed: v2 {err2:.3f} > v1 {err1:.3f}")
    # Gold-coverage guard: without enough labelled items the agreement floor is the
    # ONLY thing watching non-gold decision regressions — not enough to certify the
    # cutover, so an under-covered run FAILS rather than silently passing.
    if n_gold < min_gold:
        failures.append(f"gold set too small to certify recall/false-accept: {n_gold} < {min_gold}")

    return ParityReport(
        passed=not failures,
        gating_failures=failures,
        metrics={
            "validity": {"v1": val1, "v2": val2},
            "verdict_agreement": agreement,
            "decision_flips": decision_flips,
            "n_gold": n_gold,
            "recall": {"v1": rec1, "v2": rec2},
            "false_accept": {"v1": fa1, "v2": fa2},
            "error_rate": {"v1": err1, "v2": err2},
            "cost": {"v1": sum(r.cost for r in v1), "v2": sum(r.cost for r in v2)},  # informational
            "latency_s": {
                "v1": sum(r.latency_s for r in v1),
                "v2": sum(r.latency_s for r in v2),
            },  # informational
            "n": len(v1),
        },
    )


# ── container fidelity (G3/G4) — the S4/S5 candidate-vs-baseline gate ───────────
#
# Story da34: S4 (merge G3+G4 into one call) and S5 (bin-pack children per call) must
# clear a SEMANTIC fidelity bar before they ship — the CANDIDATE container path may not
# lose findings or mis-attribute them vs the BASELINE (separate-call G3,G4 /
# one-child-per-call). This reuses :func:`parity_report` (recall + false-accept +
# min_gold) and adds a G3/G4 ATTRIBUTION-accuracy check. The tolerance constants below
# are the canonical, documented numbers (mirrored in
# eval_specs/plan-review-container.eval.yaml `tolerance:` and README.plan-review.md).

# Container criteria whose attribution we score (G3 = child coverage, G4 = child
# consistency). A merged/packed candidate must still route each finding to the right one.
CONTAINER_CRITERIA = ("G3", "G4")
# >= 90% of correctly-caught container findings must be attributed to the right criterion.
ATTRIBUTION_ACCURACY_FLOOR = 0.90
# Container gold sets are smaller than the cutover corpus; the certifying floor is lower.
CONTAINER_MIN_GOLD = 12


def attribution_accuracy(records: Sequence[ItemRecord]) -> float:
    """Fraction of caught container findings attributed to the RIGHT G3/G4 criterion.

    Scored only over records that (a) carry a gold container criterion, (b) the runner
    actually flagged (``decision == 'block'`` — a missed finding is a recall miss, not an
    attribution error), and (c) attributed to some criterion. Returns 1.0 when there is
    nothing to score (no caught container findings)."""
    scored = [
        r
        for r in records
        if r.gold_criterion in CONTAINER_CRITERIA and r.decision == "block" and r.pred_criterion
    ]
    if not scored:
        return 1.0
    return sum(1 for r in scored if r.pred_criterion == r.gold_criterion) / len(scored)


def container_fidelity_report(
    baseline: Sequence[ItemRecord],
    candidate: Sequence[ItemRecord],
    *,
    min_gold: int = CONTAINER_MIN_GOLD,
    attribution_floor: float = ATTRIBUTION_ACCURACY_FLOOR,
) -> ParityReport:
    """The S4/S5 fidelity gate: diff a CANDIDATE container path against the BASELINE.

    Reuses :func:`parity_report` for finding-recall + false-accept non-inferiority and
    the ``min_gold`` certification floor (``baseline`` = v1, ``candidate`` = v2), then
    layers the G3/G4 ATTRIBUTION-accuracy floor on the candidate. ``passed`` is True only
    if the parity bar holds AND the candidate's attribution clears ``attribution_floor``.

    The attribution floor is deliberately ABSOLUTE on the candidate (not a
    non-inferiority margin vs the baseline): the candidate is what SHIPS, so a
    correct-routing bar of 90% must hold regardless of whether the baseline happened to
    route worse. The baseline's accuracy is still computed + reported (``acc_base``) for
    visibility/diagnosis — it informs the human review, it does not relax the gate. The
    extra ``attribution_accuracy`` figures are merged into ``metrics``."""
    report = parity_report(baseline, candidate, min_gold=min_gold)
    acc_base = attribution_accuracy(baseline)
    acc_cand = attribution_accuracy(candidate)
    failures = list(report.gating_failures)
    if acc_cand + 1e-9 < attribution_floor:
        failures.append(
            f"G3/G4 attribution accuracy {acc_cand:.3f} below floor {attribution_floor}"
        )
    report.metrics["attribution_accuracy"] = {"baseline": acc_base, "candidate": acc_cand}
    return ParityReport(passed=not failures, gating_failures=failures, metrics=report.metrics)


def prerequisite_fidelity_report(
    baseline: Sequence[ItemRecord],
    candidate: Sequence[ItemRecord],
    *,
    min_recall: float = 0.90,
    max_false_accept: float = 0.10,
    min_gold: int = 20,
) -> ParityReport:
    """Gate focused prerequisite recall, coverage and authoritative attribution."""
    report = parity_report(baseline, candidate, min_gold=min_gold)
    recall, false_accept = _recall_false_accept(candidate)
    expected = [r.gold_prerequisite_id for r in candidate if r.gold_prerequisite_id]
    predicted = [r.pred_prerequisite_id for r in candidate if r.pred_prerequisite_id]
    expected_counts = {pid: expected.count(pid) for pid in set(expected)}
    predicted_counts = {pid: predicted.count(pid) for pid in set(predicted)}
    complete = sum(
        1 for pid in expected_counts if expected_counts[pid] == 1 and predicted_counts.get(pid) == 1
    )
    completeness = complete / len(expected_counts) if expected_counts else 1.0
    caught = [r for r in candidate if r.gold_prerequisite_id and r.decision == "block"]
    attribution_errors = sum(1 for r in caught if r.pred_prerequisite_id != r.gold_prerequisite_id)
    attribution_error_rate = attribution_errors / len(caught) if caught else 0.0
    failures = list(report.gating_failures)
    if recall + 1e-9 < min_recall:
        failures.append(f"prerequisite recall {recall:.3f} below floor {min_recall:.3f}")
    if false_accept > max_false_accept + 1e-9:
        failures.append(
            f"prerequisite false acceptance {false_accept:.3f} exceeds {max_false_accept:.3f}"
        )
    if completeness < 1.0:
        failures.append(f"prerequisite coverage incomplete: {completeness:.3f}")
    if attribution_errors:
        failures.append(f"{attribution_errors} prerequisite attribution error(s)")
    metrics = dict(report.metrics)
    metrics.update(
        {
            "coverage_completeness": completeness,
            "prerequisite_attribution_error_rate": attribution_error_rate,
            "min_recall": min_recall,
            "max_false_accept": max_false_accept,
            "min_gold": min_gold,
            "required_coverage_completeness": 1.0,
            "max_prerequisite_attribution_error_rate": 0.0,
        }
    )
    return ParityReport(passed=not failures, gating_failures=failures, metrics=metrics)


def parallel_run_and_diff(corpus, run_v1, run_v2, *, to_record) -> ParityReport:
    """Live driver: run BOTH runners over ``corpus`` and diff via :func:`parity_report`.

    ``run_v1`` / ``run_v2`` take a corpus item and return that runner's raw result;
    ``to_record(item, result, errored)`` maps a raw result to an :class:`ItemRecord`
    (carrying the gold label from the item). Each item is run through both runners so
    the records are PAIRED. This is the part that needs funded model calls; the gate it
    feeds (:func:`parity_report`) is the same code exercised offline by the tests."""
    v1: list[ItemRecord] = []
    v2: list[ItemRecord] = []
    for item in corpus:
        for runner, sink in ((run_v1, v1), (run_v2, v2)):
            try:
                sink.append(to_record(item, runner(item), False))
            except Exception:  # noqa: BLE001 — parity harness: a runner failure is recorded as a failure entry (errored=True), never aborts the comparison
                sink.append(to_record(item, None, True))
    return parity_report(v1, v2)
