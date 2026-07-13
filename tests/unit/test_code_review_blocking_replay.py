"""b9c0: self-contained replay proving the `security` criterion now BLOCKs at the derived
threshold (9f25's 0.54) while low-priority NIT findings do not. Uses the real routing
(reg.threshold_for) + the gate's actual block condition (criterion blocking AND
priority >= threshold), NOT the sidecar persistence shape — no dependency on any corpus."""

from __future__ import annotations

from rebar.llm.code_review import registry as reg

DERIVED_THRESHOLD = 0.54  # 9f25's provisional priority-crossover (the #518 security band)


def _blocks(criteria: list[str], priority: float) -> bool:
    """The gate's block rule: the finding's criterion is blocking-enabled AND its priority
    (validity x impact) meets the criterion's block_threshold."""
    threshold, blocking = reg.threshold_for(criteria)
    return bool(blocking) and priority >= threshold


def test_security_criterion_is_blocking_at_derived_threshold() -> None:
    threshold, blocking = reg.threshold_for(["security"])
    assert blocking is True
    assert threshold == DERIVED_THRESHOLD


def test_518_class_security_finding_now_blocks() -> None:
    # #518: importlib code-execution — a security finding at the 0.54 band now BLOCKS.
    assert _blocks(["security"], 0.54) is True
    assert _blocks(["security"], 0.60) is True


def test_nit_priority_below_threshold_does_not_block() -> None:
    # A low-priority security-lane NIT (priority 0.32, the observed nit cluster) does NOT block.
    assert _blocks(["security"], 0.32) is False
    assert _blocks(["security"], 0.50) is False  # just under the 0.54 band
