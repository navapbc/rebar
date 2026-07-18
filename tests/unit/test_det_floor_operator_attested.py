"""Unit tests for the operator-attested evidence-kind advisory DET lint (ticket b080,
epic 6982; ADR-0043 x ADR-0016).

The lint extends :func:`p6_ac_quality` — it is ADVISORY (never blocks) and prompt-less
(a lexicon over AC text). It flags AC checklist items whose "done" evidence lives OUTSIDE
the codebase (deploy / prod / live-run / IaC / cloud-state / merge-gate / human action /
drill / store-surgery / recorded attestation) but which are NOT tagged
``[operator-attested]``. Self-gated by the deterministic lexicon eval under
``docs/experiments/plan-review-gate/`` (>=70% precision, <=5% flag rate, both known cases).
"""

from __future__ import annotations

import pytest

from rebar.llm.plan_review import registry, workflow_ops
from rebar.llm.plan_review.det_floor import (
    _OPERATOR_ATTESTED_TAG_RE,
    DET_CHECKS,
    PlanContext,
    _operator_evidence_ac_gaps,
    p6_ac_quality,
)


def _ac(*lines: str) -> str:
    return "## Acceptance Criteria\n" + "\n".join(lines) + "\n"


# (label, ac_line, should_fire). The 115b/8c4f rows are the historical (tag-stripped) AC
# text of the two tickets that motivated this lint — they MUST fire. The negatives exercise
# the codebase-verifiable SUPPRESSION (proving command / doc / "— landed on main" trailer)
# and the NEGATION guard ("no live") — a marker word alone must not flag them.
_FIXTURES = [
    (
        "115b-retire",
        "- [ ] Per-orphan disposition applied: retire each of the 13 orphan EDIT "
        "files (`.json` -> `.json.retired`) against the live shared store",
        True,
    ),
    (
        "115b-phantom",
        "- [ ] MISSING_CREATE disposition applied: delete the 4 local phantom dirs",
        True,
    ),
    (
        "8c4f-fsck",
        "- [ ] Re-run `rebar fsck` with the ENQUEUE_ENRICH-era binary against the live "
        "store and record the counts",
        True,
    ),
    ("deploy", "- [ ] the fix is deployed to prod and the gate passes", True),
    (
        "landed-gerrit",
        "- [ ] the change lands on main through Gerrit (LLM-Review +1 and Verified +1)",
        True,
    ),
    ("terraform", "- [ ] terraform apply completed and terraform plan reports no changes", True),
    ("sns", "- [ ] the SNS subscription is confirmed and delivering", True),
    ("operator", "- [ ] the operator configures the JIRA_URL repo variables", True),
    # negatives — codebase-verifiable / negated, must NOT fire
    (
        "suppressed-test",
        "- [ ] terraform apply is exercised by `pytest tests/test_infra.py -q`",
        False,
    ),
    ("landed-trailer", "- [ ] a pure classifier encodes the state matrix — landed on main", False),
    ("negated-live", "- [ ] convergence holds with no live store (offline fixture)", False),
    ("plain-code", "- [ ] add the parser in `src/rebar/x.py` and cover the edge case", False),
    ("doc", "- [ ] docs/architecture.md documents the new seam", False),
]


@pytest.mark.parametrize("label,line,should_fire", _FIXTURES, ids=[f[0] for f in _FIXTURES])
def test_operator_evidence_fixtures(label: str, line: str, should_fire: bool) -> None:
    assert bool(_operator_evidence_ac_gaps(_ac(line))) is should_fire


def test_known_case_115b_fires() -> None:
    """115b (live-store fsck surgery) — the historical untagged AC must be flagged."""
    text = _ac("- [ ] retire each of the 13 orphan EDIT files against the live shared store")
    gaps = _operator_evidence_ac_gaps(text)
    assert gaps and "store_op" in gaps[0][1]


def test_known_case_8c4f_fires() -> None:
    """8c4f (re-run fsck against the live store) — the historical untagged AC must be flagged."""
    text = _ac("- [ ] Re-run `rebar fsck` against the live store and record the counts")
    assert _operator_evidence_ac_gaps(text)


def test_tagged_item_not_flagged() -> None:
    """An AC already tagged [operator-attested] declares its out-of-codebase evidence — skip it."""
    tagged = _ac("- [ ] [operator-attested] the fix is deployed to prod and the gate passes")
    assert _operator_evidence_ac_gaps(tagged) == []


def test_near_miss_tag_still_flagged() -> None:
    """A malformed near-miss tag ([operator_attested]) is NOT the tag (ADR-0043 exact match),
    so the operational AC is still flagged."""
    near = _ac("- [ ] [operator_attested] the fix is deployed to prod")
    assert _operator_evidence_ac_gaps(near)


def test_p6_operator_attested_is_advisory() -> None:
    """The lint is surfaced through p6, which fails (advisory) but NEVER blocks."""
    ctx = PlanContext(
        ticket_id="t",
        ticket_type="task",
        title="T",
        description=_ac("- [ ] the fix is deployed to prod and the gate passes"),
    )
    r = p6_ac_quality(ctx)
    assert r.status == "fail" and r.blocking is False
    assert r.coverage["operator_attested_gaps"] == 1
    assert r.finding is not None
    assert any("[operator-attested]" in e for e in r.finding["evidence"])


def test_p6_clean_when_operational_ac_tagged() -> None:
    """A well-formed plan whose only operational AC is tagged has no operator-attested gap."""
    ctx = PlanContext(
        ticket_id="t",
        ticket_type="task",
        title="T",
        description=_ac(
            "- [ ] [operator-attested] the fix is deployed to prod and the gate passes",
            "- [ ] the parser is added in `src/rebar/x.py`; proof: `pytest tests/x.py -q`",
        ),
    )
    assert p6_ac_quality(ctx).coverage["operator_attested_gaps"] == 0


def test_det_checks_stay_p1_to_p9() -> None:
    """The lint EXTENDS p6 (like the verify-command lint) — it must NOT add a P10."""
    assert len(DET_CHECKS) == 9


def test_canonical_det_unchanged_and_no_routing_orphan() -> None:
    """DET floor checks are not routed through criteria_routing.json; the packaged-routing CI
    gate must stay clean (no ORPHAN) — i.e. no DET entry was added to the routing index."""
    assert list(registry.CANONICAL_DET) == [f"P{i}" for i in range(1, 10)]
    assert registry.validate_packaged_routing() == []


def test_tag_regex_single_source() -> None:
    """det_floor OWNS the [operator-attested] matcher; workflow_ops re-exports it (identity)."""
    assert workflow_ops._OPERATOR_ATTESTED_AC_RE is _OPERATOR_ATTESTED_TAG_RE


def test_agreement_with_workflow_ops() -> None:
    """A line workflow_ops recognizes as tagged is never flagged by the plan-time lint — the
    lint and the completion-verifier enrichment agree on 'tagged' by construction."""
    for line in (
        "- [ ] [operator-attested] the fix is deployed to prod",
        "- [ ] [OPERATOR-ATTESTED] the change is merged to main via Gerrit",
    ):
        assert workflow_ops.operator_attested_ac_texts(line)
        assert _operator_evidence_ac_gaps(_ac(line)) == []
