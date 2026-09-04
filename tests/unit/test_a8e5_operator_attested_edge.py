"""HELD-OUT edge tests for a8e5 Component 3 (operator-attested AC awareness). Merge into
tests/unit/test_plan_review.py after the implementer has only seen the happy path."""

import pytest

from rebar.llm import review_kernel
from rebar.llm.plan_review.decide_ops import (
    enrich_operator_attested,
    operator_attested_ac_texts,
)

pytestmark = pytest.mark.unit

# ADR 0101 renamed the acceptance-criterion tag to `[non-codebase]`, keeping
# `[operator-attested]` as an accepted compatibility alias. The enrichment edge cases below
# run under BOTH spellings so the alias has an explicit regression suite, rather than the
# legacy cases merely continuing to pass by accident.
_TAGS = ("[non-codebase]", "[operator-attested]")


def test_parser_is_case_insensitive_on_the_token() -> None:
    desc = "## Acceptance Criteria\n- [ ] [OPERATOR-ATTESTED] the drill is run in prod quarterly\n"
    texts = operator_attested_ac_texts(desc)
    assert len(texts) == 1 and "drill is run" in texts[0].lower()


def test_parser_rejects_underscore_near_miss() -> None:
    # ADR-0043: matching is exact on the hyphenated token; `[operator_attested]` is NOT it.
    desc = "## Acceptance Criteria\n- [ ] [operator_attested] not a real tag\n"
    assert operator_attested_ac_texts(desc) == []


def test_parser_ignores_untagged_criteria() -> None:
    desc = (
        "## Acceptance Criteria\n- [ ] plain criterion one\n- [x] plain criterion two (checked)\n"
    )
    assert operator_attested_ac_texts(desc) == []


@pytest.mark.parametrize("tag", _TAGS)
def test_enrich_leaves_non_operator_attested_ac_finding_alone(tag: str) -> None:
    # A finding flagging a NON-operator-attested AC keeps its ac_unverifiable (still floored).
    desc = (
        "## Acceptance Criteria\n"
        "- [ ] the parser handles empty input\n"
        f"- [ ] {tag} the deploy is confirmed live\n"
    )
    findings = [
        {
            "finding": "AC unverifiable",
            "location": "## Acceptance Criteria",
            "evidence": ["the parser handles empty input"],  # references the NON-OA AC
        }
    ]
    verifs = {0: {"severity_attributes": {"ac_unverifiable": "missing_oracle"}, "binary": {}}}
    enrich_operator_attested(findings, verifs, desc)
    attrs = verifs[0]["severity_attributes"]
    assert attrs.get("ac_unverifiable") == "missing_oracle"  # untouched
    assert not attrs.get("operator_attested")
    assert review_kernel.impact_plan(attrs) >= 0.85


def test_enrich_underscore_near_miss_does_not_clear() -> None:
    desc = "## Acceptance Criteria\n- [ ] [operator_attested] the deploy is confirmed live\n"
    findings = [{"finding": "x", "evidence": ["the deploy is confirmed live"], "location": "AC"}]
    verifs = {0: {"severity_attributes": {"ac_unverifiable": "missing_oracle"}, "binary": {}}}
    enrich_operator_attested(findings, verifs, desc)
    assert verifs[0]["severity_attributes"].get("ac_unverifiable") == "missing_oracle"


@pytest.mark.parametrize("tag", _TAGS)
def test_enrich_is_a_noop_when_no_finding_references_the_oa_ac(tag: str) -> None:
    desc = f"## Acceptance Criteria\n- [ ] {tag} the deploy is confirmed live\n"
    findings = [
        {"finding": "unrelated concern about naming", "evidence": ["src/x.py"], "location": "x"}
    ]
    verifs = {0: {"severity_attributes": {"ac_unverifiable": "missing_oracle"}, "binary": {}}}
    enrich_operator_attested(findings, verifs, desc)
    assert verifs[0]["severity_attributes"].get("ac_unverifiable") == "missing_oracle"


@pytest.mark.parametrize("tag", _TAGS)
def test_enrich_handles_missing_verif_and_bad_shapes(tag: str) -> None:
    # Fail-safe: a finding with no verification, or a non-dict severity_attributes, never crashes.
    desc = f"## Acceptance Criteria\n- [ ] {tag} the deploy is confirmed live\n"
    findings = [
        {"finding": "the deploy is confirmed live", "evidence": ["the deploy is confirmed live"]},
        {"finding": "no verif"},
    ]
    # index 1 is absent from verifs (a finding with no verification)
    verifs = {0: {"severity_attributes": {"ac_unverifiable": "missing_oracle"}, "binary": {}}}
    enrich_operator_attested(findings, verifs, desc)  # must not raise
    assert verifs[0]["severity_attributes"].get("ac_unverifiable") == "none"


@pytest.mark.parametrize("tag", _TAGS)
def test_enrich_no_ac_unverifiable_still_flags_but_impact_unchanged(tag: str) -> None:
    # A finding referencing the OA AC but with ac_unverifiable already "none": operator_attested
    # may be flagged (observability) and impact stays 0.0 — nothing to clear, no crash.
    desc = f"## Acceptance Criteria\n- [ ] {tag} the deploy is confirmed live\n"
    findings = [{"finding": "x", "evidence": ["the deploy is confirmed live"], "location": "AC"}]
    verifs = {0: {"severity_attributes": {}, "binary": {}}}
    enrich_operator_attested(findings, verifs, desc)
    assert review_kernel.impact_plan(verifs[0]["severity_attributes"]) < 0.85


@pytest.mark.parametrize("tag", _TAGS)
def test_operator_attested_finding_survives_on_other_axes(tag: str) -> None:
    """Clearing ac_unverifiable removes ONLY that axis's hard-override contribution — a finding
    that ALSO scores on another axis (here divergent_implementation=contradicts_reality, a
    plan-v4 floor grade) still produces impact and survives (the clear is not a blanket drop of
    the finding)."""
    desc = f"## Acceptance Criteria\n- [ ] {tag} the deploy is confirmed live\n"
    findings = [
        {
            "finding": "the AC cannot be verified in-session AND the plan diverges from the parent",
            "location": "## Acceptance Criteria",
            "evidence": ["the deploy is confirmed live"],
        }
    ]
    verifs = {
        0: {
            "severity_attributes": {
                "ac_unverifiable": "missing_oracle",
                "divergent_implementation": "contradicts_reality",
            },
            "binary": {},
        }
    }
    enrich_operator_attested(findings, verifs, desc)
    attrs = verifs[0]["severity_attributes"]
    assert attrs.get("operator_attested") is True
    assert attrs.get("ac_unverifiable") == "none"  # the OA axis is cleared
    # the other axis is untouched — and a floor grade is NEVER cleared by attestation (plan-v4)
    assert attrs.get("divergent_implementation") == "contradicts_reality"
    # the finding still scores (divergent_implementation is a hard-override axis → floor 0.85)
    assert review_kernel.impact_plan(attrs) >= 0.85
