"""Plan-review threshold + posture recalibration (story usable-chattery-coelacanth).

The approved pre-redesign changes to `criteria_routing.json`:
  - lower block_threshold 0.70 -> 0.60 for COH, E2, F1, G5, G6, T5e;
  - keep T4 @ 0.70 (blocking);
  - promote to blocking: G1G2 @ 0.70, T1 @ 0.70, T8 @ 0.70, E4 @ 0.75.
No other criterion changes.

Amended by calibration 3 (task relishable-ammonitic-hoverfly, plan-v2 segmented replay):
  - T5e demoted to advisory @ 0.95 (FP-PRONE: validity 0.391, verifier drops 59% of its
    findings, surviving p90 priority 0.27). The other ten blocking criteria KEEP their
    calibration-2 thresholds; see docs/experiments/plan-review-threshold-calibration.md
    "Calibration 3".

Proving command:
    .venv/bin/pytest tests/unit/test_threshold_recalibration.py -v
"""

from __future__ import annotations

from rebar.llm.criteria.model import threshold_for
from rebar.llm.plan_review import registry

# The SIX lowered at calibration 2 (0.70 -> 0.60) MINUS T5e, demoted to advisory at
# calibration 3 (see module docstring).
LOWERED = {"COH", "E2", "F1", "G5", "G6"}
CAL3_DEMOTED = {"T5e"}
# The FOUR promoted to default_posture=blocking, with their new thresholds.
PROMOTED = {"G1G2": 0.70, "T1": 0.70, "T8": 0.70, "E4": 0.75}

# AC4: the COMPLETE expected routing, pinned INLINE (no separate snapshot file). Any unintended
# change to ANY criterion's (block_threshold, default_posture) fails the assertion below.
EXPECTED_ROUTING: dict[str, tuple[float, str]] = {
    "A1": (0.95, "advisory"),
    "COH": (0.6, "blocking"),
    "E1": (0.95, "advisory"),
    "E2": (0.6, "blocking"),
    "E3": (0.95, "advisory"),
    "E4": (0.75, "blocking"),
    "E5": (0.95, "advisory"),
    "E6": (0.95, "advisory"),
    "F1": (0.6, "blocking"),
    "F4": (0.95, "advisory"),
    "G1G2": (0.7, "blocking"),
    "G3": (0.95, "advisory"),
    "G4": (0.95, "advisory"),
    "G5": (0.6, "blocking"),
    "G6": (0.6, "blocking"),
    "G7": (0.95, "advisory"),
    "ISF": (0.95, "advisory"),
    "T1": (0.7, "blocking"),
    "T10": (0.95, "advisory"),
    "T11": (0.95, "advisory"),
    "T12": (0.95, "advisory"),
    "T13": (0.95, "advisory"),
    "T14": (0.95, "advisory"),
    "T2": (0.95, "advisory"),
    "T3": (0.95, "advisory"),
    "T4": (0.7, "blocking"),
    "T5a": (0.95, "advisory"),
    "T5b": (0.95, "advisory"),
    "T5c": (0.95, "advisory"),
    "T5d": (0.95, "advisory"),
    "T5e": (0.95, "advisory"),
    "T6": (0.95, "advisory"),
    "T7": (0.95, "advisory"),
    "T8": (0.7, "blocking"),
    "T9": (0.95, "advisory"),
    "hedge": (0.95, "advisory"),
    "removal-rationale": (0.95, "advisory"),
}


def _routing() -> dict:
    # Built-in routing only — these pins govern the packaged built-in criteria table.
    # Exclude any activated project.* overlay criterion (e.g. the dogfood
    # project.portability), which the ambient real-repo `.rebar/` overlay merges in but
    # is out of scope for the built-in threshold recalibration this module verifies.
    return {cid: v for cid, v in registry.by_id().items() if not cid.startswith("project.")}


# ── AC1: the approved 11 criteria match the table exactly ────────────────────────────────
def test_six_lowered_criteria_at_060_blocking() -> None:
    r = _routing()
    for cid in LOWERED:
        assert r[cid]["block_threshold"] == 0.6, cid
        assert r[cid]["default_posture"] == "blocking", cid


def test_cal3_demoted_t5e_is_advisory_at_095() -> None:
    r = _routing()
    for cid in CAL3_DEMOTED:
        assert r[cid]["block_threshold"] == 0.95, cid
        assert r[cid]["default_posture"] == "advisory", cid


def test_t4_unchanged_at_070_blocking() -> None:
    r = _routing()
    assert r["T4"]["block_threshold"] == 0.7
    assert r["T4"]["default_posture"] == "blocking"


def test_four_promoted_criteria_are_blocking_at_new_thresholds() -> None:
    r = _routing()
    for cid, thr in PROMOTED.items():
        assert r[cid]["default_posture"] == "blocking", cid
        assert r[cid]["block_threshold"] == thr, cid


# ── AC3: deterministic simulation of the recalibration's effect (no live corpus) ─────────
def test_lowered_criteria_block_a_065_finding_that_070_would_strand() -> None:
    r = _routing()
    for cid in LOWERED:
        thr, blocking = threshold_for([cid], r, gate="plan_review")
        assert (thr, blocking) == (0.6, True), cid
        # a would-block finding at priority 0.65: blocks now (>=0.60), stranded under the old 0.70
        assert 0.65 >= thr, cid
        assert 0.65 < 0.70, cid


def test_promoted_criteria_are_blocking_but_only_above_their_new_bar() -> None:
    r = _routing()
    for cid, thr_expected in PROMOTED.items():
        thr, blocking = threshold_for([cid], r, gate="plan_review")
        assert blocking is True, cid
        assert thr == thr_expected, cid
        # a 0.65 finding does NOT block for a criterion promoted at 0.70/0.75
        assert 0.65 < thr, cid


# ── AC4: no criterion OUTSIDE the approved set changed (full-table regression pin) ───────
def test_full_routing_table_matches_pinned_expectation() -> None:
    r = _routing()
    actual = {cid: (v.get("block_threshold"), v.get("default_posture")) for cid, v in r.items()}
    assert actual == EXPECTED_ROUTING


def test_only_the_eleven_approved_criteria_are_blocking() -> None:
    # The blocking set is exactly the approved criteria; nothing else silently flipped posture.
    r = _routing()
    blocking = {cid for cid, v in r.items() if v.get("default_posture") == "blocking"}
    assert blocking == LOWERED | set(PROMOTED) | {"T4"}
