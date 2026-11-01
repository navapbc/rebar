"""The completion verifier's [non-codebase] remediation matches the close contract (bug 81f2).

Following ``COMPLETION_REMEDIATION_GUIDANCE`` used to cost three refused closes and two fresh
plan reviews: the text told the author to add the tag, but not where it goes, not that the
close precheck also requires a ``provenance:`` continuation line, and not that the edit is a
material plan change that stales the plan-review attestation. The tag stays material by
design (tickets f680 and 2f56: retagging after certification is the laundering vector), so
the fix is the guidance. These tests tie the guidance to the contract the close precheck
enforces, so the two cannot drift apart again.
"""

from __future__ import annotations

import re

from rebar.llm.completion import COMPLETION_REMEDIATION_GUIDANCE
from rebar.llm.plan_review.det_attestation_launder import laundering_gaps
from rebar.llm.plan_review.det_floor import PlanContext
from rebar.llm.plan_review.det_measurement_provenance import (
    INSTRUMENTS,
    PRIVILEGE_POSTURES,
    PROVENANCE_KEYS,
    provenance_gaps,
)
from rebar.llm.plan_review.det_operator_attested import _OPERATOR_ATTESTED_TAG_RE
from rebar.llm.plan_review.pass1 import material_fingerprint

_CRITERION = "The change lands on main with Verified +1."
_FILLED_PROVENANCE = (
    "  provenance: environment=rebar.solutions.navateam.com (Gerrit); principal=landing bot; "
    "privilege_posture=production-equivalent; instrument=live-call — votes read from the "
    "change detail before submit"
)


def _ac(*lines: str) -> str:
    return "## Acceptance Criteria\n\n" + "\n".join(lines) + "\n"


def _fingerprint(description: str) -> str:
    ctx = PlanContext(
        ticket_id="81f2-68a2-5710-4f9a",
        ticket_type="bug",
        title="t",
        description=description,
        state={"file_impact": []},
        children=[],
    )
    return material_fingerprint(ctx)


def _example_checkbox_lines() -> list[str]:
    """Every backticked checkbox example the guidance shows."""
    return [m for m in re.findall(r"`([^`]+)`", COMPLETION_REMEDIATION_GUIDANCE) if "- [ ]" in m]


def test_guidance_shows_the_tag_at_the_start_of_the_checkbox_text() -> None:
    examples = _example_checkbox_lines()
    assert examples, "the guidance must show where the tag goes"
    assert all(_OPERATOR_ATTESTED_TAG_RE.match(e) for e in examples), examples
    assert "start of the checkbox text" in COMPLETION_REMEDIATION_GUIDANCE


def test_guidance_names_the_provenance_line_the_close_precheck_requires() -> None:
    assert "provenance:" in COMPLETION_REMEDIATION_GUIDANCE
    for token in (*PROVENANCE_KEYS, *PRIVILEGE_POSTURES, *INSTRUMENTS):
        assert token in COMPLETION_REMEDIATION_GUIDANCE, token


def test_an_edit_made_as_the_guidance_describes_passes_both_close_guards() -> None:
    description = _ac(f"- [ ] [non-codebase] {_CRITERION}", _FILLED_PROVENANCE)
    assert provenance_gaps(description) == []
    assert laundering_gaps(description) == []


def test_guidance_says_the_edit_stales_the_plan_review_attestation() -> None:
    lowered = COMPLETION_REMEDIATION_GUIDANCE.lower()
    assert "rebar review-plan" in lowered
    assert "stales the plan-review attestation" in lowered
    # The non-material route for a criterion tagged before review is still named.
    assert "non-material" in lowered


def test_tick_is_safe_but_tag_provenance_and_text_edits_stay_material() -> None:
    base = _fingerprint(_ac(f"- [ ] {_CRITERION}"))
    assert _fingerprint(_ac(f"- [x] {_CRITERION}")) == base

    tagged = _fingerprint(_ac(f"- [ ] [non-codebase] {_CRITERION}"))
    assert tagged != base
    with_provenance = _fingerprint(_ac(f"- [ ] [non-codebase] {_CRITERION}", _FILLED_PROVENANCE))
    assert with_provenance not in (base, tagged)
    reworded = _fingerprint(_ac("- [ ] The change lands on main with Verified +2."))
    assert reworded != base
