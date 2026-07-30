"""G7 blocking enablement at 0.85 plus the rubric abstain/dedup fixes (ticket 28d5).

FP verification over all 348 historical G7 findings (ticket 696a): at priority >= 0.85,
0 clearly-incorrect findings; the 0.80-0.85 band is empty, so 0.85 selects the same
historical set as 0.80 with less future exposure. Plan-review blocking derives from
``default_posture: "blocking"`` + ``block_threshold`` (``blocking_enabled`` is the
code-review gate's convention and is inert here) — the bfa8/c97a pattern: a routing flip
plus rubric guidance, no ``pass3_decide`` changes, no per-grade machinery.

Kind scoping comes from the EXISTING plan-v4 divergence grading (ADR 0054): G7 maps its
severity onto the ``divergent_implementation`` axis, where ``contradicts_reality`` and
``omits_required_site`` score 1.0 and hard-floor impact at 0.85, while
``incomplete_enumeration`` contributes 0.55 (strictly below every blocking threshold)
and never floors. These tests drive findings through the REAL plan-review Pass-3
wrapper with the REAL ``impact_plan`` — the kind grades themselves produce the impact.

Also pinned: the four new rubric rule sentences — abstain-on-unresolvable-parent
(never a finding), closed-parent-final-state + sibling-handoff, one finding per
contradicted parent clause, and the floor-kind guidance.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rebar.llm.criteria.model import threshold_for
from rebar.llm.plan_review import orchestrator, registry
from rebar.llm.review_kernel import GRADED_BINARY

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).parents[3]

# An all-"yes" graded binary: validity == 1.0, so a finding's priority equals the impact
# the real impact_plan computes from the injected divergence grade.
_ALL_YES = {q: "yes" for q in GRADED_BINARY}


def _decided(grade: str, *, binary: dict | None = None, self_revealing: bool = False) -> dict:
    """One G7 finding through the REAL plan-review Pass-3 wrapper with the REAL
    ``impact_plan`` (no impact monkeypatch): the ``divergent_implementation`` grade is
    the only severity attribute, so the decision exercises the plan-v4 kind scoping
    end to end."""
    attrs: dict = {"divergent_implementation": grade}
    if self_revealing:
        attrs["silent_vs_self_revealing"] = "self_revealing"
    finding = {"criteria": ["G7"], "finding": "fixture finding"}
    verifs = {0: {"binary": dict(binary or _ALL_YES), "severity_attributes": attrs}}
    return orchestrator.pass3_over_findings([finding], verifs)[0]


# ── routing posture: blocking @ 0.85 ──────────────────────────────────────────
def test_routing_posture_blocking_at_085() -> None:
    entry = registry.by_id()["G7"]
    assert entry["default_posture"] == "blocking"
    assert entry["block_threshold"] == 0.85

    thr, blocking = threshold_for(["G7"], registry.by_id(), gate="plan_review")
    assert (thr, blocking) == (0.85, True)


# ── Pass-3 with the real impact_plan: floor kinds block, cosmetic kind stays ──
def test_contradicts_reality_at_full_validity_blocks() -> None:
    d = _decided("contradicts_reality")
    assert d["validity"] == 1.0
    assert d["impact"] == 1.0
    assert d["priority"] == 1.0
    assert d["decision"] == "block"
    assert d["block_threshold"] == 0.85
    assert d["blocking_enabled"] is True


def test_contradicts_reality_self_revealing_floors_to_085_and_blocks() -> None:
    # The exact-boundary case: the detection amplifier (0.8) is applied, then the hard
    # override floors impact back to 0.85 — priority 0.85 >= block_threshold 0.85.
    d = _decided("contradicts_reality", self_revealing=True)
    assert d["impact"] == 0.85
    assert d["priority"] == 0.85
    assert d["decision"] == "block"


def test_omits_required_site_at_full_validity_blocks() -> None:
    # Deliberately pinned: BOTH floor-grade kinds block. ADR 0054 gives
    # omits_required_site the 0.85 hard floor; restricting blocking to
    # contradicts_reality alone would need per-grade Pass-3 gating that does not exist.
    d = _decided("omits_required_site")
    assert d["impact"] == 1.0
    assert d["priority"] == 1.0
    assert d["decision"] == "block"


def test_incomplete_enumeration_stays_advisory_at_055() -> None:
    # The cosmetic kind contributes DIVERGENCE_INCOMPLETE_CONTRIB (0.55) and never
    # floors, so it cannot reach the 0.85 bar through the divergence axis.
    d = _decided("incomplete_enumeration")
    assert d["impact"] == 0.55
    assert d["priority"] == 0.55
    assert d["decision"] == "advisory"


def test_floor_kind_with_degraded_validity_stays_advisory() -> None:
    # The validity guard: 9 answerable sub-answers with one "no" → validity 8/9 =
    # 0.8889; with the self-revealing floor impact 0.85 the priority is 0.7556 < 0.85.
    binary = {q: "yes" for q in GRADED_BINARY[:9]}
    binary[GRADED_BINARY[0]] = "no"
    d = _decided("contradicts_reality", binary=binary, self_revealing=True)
    assert d["validity"] == 0.8889
    assert d["priority"] == 0.7556
    assert d["decision"] == "advisory"


# ── the rubric's new rule sentences ───────────────────────────────────────────
def _rubric() -> str:
    return (_ROOT / "src/rebar/llm/reviewers/plan_review_G7.md").read_text(encoding="utf-8")


def test_rubric_contains_abstain_on_unresolvable_parent() -> None:
    text = _rubric()
    assert "FAIL-OPEN (abstain-with-coverage)" in text
    assert "covered-but-unverified" in text
    assert "never a finding" in text


def test_rubric_contains_closed_parent_and_sibling_handoff_rules() -> None:
    text = _rubric()
    assert "FINAL (as-closed) state" in text
    assert "explicitly handed to sibling tickets is NOT a leaf violation" in text


def test_rubric_contains_one_finding_per_contradicted_parent_clause() -> None:
    text = _rubric()
    assert "DEDUP-AT-SOURCE" in text
    assert "at most ONE finding per contradicted parent clause" in text


def test_rubric_contains_floor_kind_guidance() -> None:
    text = _rubric()
    assert "ONLY a genuine contract contradiction or a provably required omission" in text
    assert "coached, never auto-blocked" in text
