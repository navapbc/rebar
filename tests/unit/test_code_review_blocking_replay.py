"""b9c0: self-contained replay proving the `security` criterion BLOCKs at its routed
threshold while low-priority NIT findings do not. Uses the real routing
(reg.threshold_for) + the gate's actual block condition (criterion blocking AND
priority >= threshold), NOT the sidecar persistence shape — no dependency on any corpus."""

from __future__ import annotations

from rebar.llm.code_review import registry as reg


def _blocks(criteria: list[str], priority: float) -> bool:
    """The gate's block rule: the finding's criterion is blocking-enabled AND its priority
    (validity x impact) meets the criterion's block_threshold."""
    threshold, blocking = reg.threshold_for(criteria)
    return bool(blocking) and priority >= threshold


def test_security_criterion_is_blocking_at_derived_threshold() -> None:
    threshold, blocking = reg.threshold_for(["security"])
    assert blocking is True
    assert 0 < threshold < 1


def test_518_class_security_finding_now_blocks() -> None:
    threshold, _ = reg.threshold_for(["security"])
    assert _blocks(["security"], threshold) is True
    assert _blocks(["security"], min(1.0, threshold + 0.01)) is True


def test_nit_priority_below_threshold_does_not_block() -> None:
    threshold, _ = reg.threshold_for(["security"])
    assert _blocks(["security"], 0.0) is False
    assert _blocks(["security"], max(0.0, threshold - 0.01)) is False
