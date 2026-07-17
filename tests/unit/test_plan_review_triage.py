"""R6 (epic 6982): deterministic advisory triage.

Covers ``passes.triage_advisories`` (the pure ranking function) and its wiring into the
``plan_review_coach`` step's ``verdict["triage"]``. The triage buckets the round's surviving
ADVISORY findings into ``apply-now`` vs ``defer`` from recorded fields alone (no LLM), so the
same finding set yields byte-identical output. See docs/plan-review-gate.md "Advisory triage".
"""

from __future__ import annotations

import json

from rebar.llm.plan_review import passes
from rebar.llm.review_kernel.decide import DEFAULT_BLOCK_THRESHOLD


def _adv(fid, priority, *, block_threshold=0.60, criteria=("E1",), decision="advisory"):
    """A decided-finding fixture in the shape ``triage_advisories`` consumes. ``block_threshold``
    None omits the key (the DET-tier advisory case)."""
    f = {"id": fid, "priority": priority, "criteria": list(criteria), "decision": decision}
    if block_threshold is not None:
        f["block_threshold"] = block_threshold
    return f


def test_determinism_byte_identical():
    findings = [_adv("a", 0.55), _adv("b", 0.20), _adv("c", 0.55, criteria=["E2"])]
    out1 = passes.triage_advisories(findings)
    out2 = passes.triage_advisories(findings)
    assert out1 == out2
    assert json.dumps(out1, sort_keys=True) == json.dumps(out2, sort_keys=True)
    for e in out1:
        # Prose is a locked format built only from recorded numbers — never LLM/prescriptive.
        if e["bucket"] == "apply-now":
            assert e["reason"] == ""
        else:
            assert e["reason"].startswith("deferred: priority ")
            assert "block line" in e["reason"]


def test_boundary_bucketing():
    bt = 0.60
    # Exactly on the boundary (priority == block_threshold - APPLY_NOW_MARGIN) -> apply-now;
    # one epsilon below -> defer. Both use the same subtraction so the comparison is exact.
    at = _adv("at", bt - passes.APPLY_NOW_MARGIN, block_threshold=bt)
    below = _adv("below", bt - passes.APPLY_NOW_MARGIN - 1e-9, block_threshold=bt)
    out = {e["id"]: e for e in passes.triage_advisories([at, below])}
    assert out["at"]["bucket"] == "apply-now"
    assert out["below"]["bucket"] == "defer"


def test_ordering_ties_and_empty_criteria():
    # priority DESC, then criteria[0] ASC (empty -> sentinel "~", sorts last), then id ASC.
    findings = [
        _adv("z", 0.5, criteria=["E1"]),
        _adv("a", 0.5, criteria=["E1"]),  # same crit+priority as z -> id ASC: a before z
        _adv("m", 0.5, criteria=["A9"]),  # crit "A9" < "E1" -> first among the 0.5 group
        _adv("n", 0.5, criteria=[]),  # empty criteria -> "~" -> last among the 0.5 group
        _adv("hi", 0.9, criteria=["Z9"]),  # highest priority -> first overall
    ]
    order = [e["id"] for e in passes.triage_advisories(findings)]
    assert order == ["hi", "m", "a", "z", "n"]


def test_advisory_only_each_appears_once():
    findings = [
        _adv("adv1", 0.5),
        {"id": "blk", "priority": 1.0, "criteria": ["E1"], "decision": "block"},
        _adv("adv2", 0.2, criteria=["E2"]),
        {"id": "ind", "priority": 0.3, "criteria": ["E1"], "decision": "indeterminate"},
    ]
    out = passes.triage_advisories(findings)
    ids = [e["id"] for e in out]
    assert ids == ["adv1", "adv2"]  # only advisories, each exactly once, ordered
    assert "blk" not in ids and "ind" not in ids


def test_det_tier_advisory_without_block_threshold_defers():
    # DET-tier advisories (priority 0.4, no block_threshold) fall back to DEFAULT (0.95) -> defer.
    det = _adv("det", 0.4, block_threshold=None, criteria=["P6"])
    assert "block_threshold" not in det
    out = passes.triage_advisories([det])
    assert out[0]["bucket"] == "defer"
    assert out[0]["block_threshold"] == DEFAULT_BLOCK_THRESHOLD


def test_integration_verdict_carries_triage():
    from rebar.llm.workflow import steps as _steps  # noqa: F401 — registers the plan-review steps
    from rebar.llm.workflow.executor import STEP_REGISTRY, StepContext

    surfaced = [_adv("a", 0.55), _adv("b", 0.20, criteria=["E2"])]
    ctx = StepContext(
        run_id="r",
        step_id="coach",
        kind="scripted",
        step={},
        inputs={
            "canonical_id": "test-triage-0000",
            "ticket_type": "task",
            "blocking": [],
            "surfaced": surfaced,
            "overflow": [],
            "indeterminate": [],
            "dropped": [],
            "notes": [],
            "det_coverage": {},
            "routing": {},
        },
        workflow={},
        target_ticket="test-triage-0000",
        repo_root=None,
    )
    op = STEP_REGISTRY["plan_review_coach"]
    out = op(ctx)
    assert out["triage"] == passes.triage_advisories(surfaced)
    assert [e["id"] for e in out["triage"]] == ["a", "b"]  # priority 0.55 before 0.20
