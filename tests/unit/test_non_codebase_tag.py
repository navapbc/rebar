"""Canonical `[non-codebase]` AC tag — happy path (story 3726, ADR 0101 amending ADR 0043).

`[non-codebase]` is the canonical spelling of the acceptance-criterion qualifier that declares
a criterion's done-evidence lives OUTSIDE the codebase. `[operator-attested]` remains accepted
as an undocumented compatibility alias. These are the happy-path cases: the new spelling is
recognized everywhere the old one is.
"""

from __future__ import annotations

from rebar.llm.plan_review.det_operator_attested import (
    _OPERATOR_ATTESTED_TAG_RE,
    ac_item_lines,
    operator_evidence_ac_gaps,
)


def _ac(*items: str) -> str:
    return "Body.\n\n## Acceptance Criteria\n" + "\n".join(items) + "\n"


def _gaps(text: str) -> list[tuple[str, list[str]]]:
    return operator_evidence_ac_gaps(ac_item_lines(text))


def test_matcher_accepts_non_codebase() -> None:
    """The canonical matcher recognizes a `[non-codebase]`-tagged checklist item."""
    assert _OPERATOR_ATTESTED_TAG_RE.match("- [ ] [non-codebase] the fix is deployed to prod")


def test_matcher_still_accepts_the_legacy_alias() -> None:
    """`[operator-attested]` keeps working — 827 of 4,285 live tickets carry it."""
    assert _OPERATOR_ATTESTED_TAG_RE.match("- [ ] [operator-attested] the fix is deployed to prod")


def test_non_codebase_tagged_operational_ac_is_not_flagged() -> None:
    """A `[non-codebase]`-tagged operational AC declares its out-of-codebase evidence, so the
    plan-time lint skips it exactly as it skips the legacy spelling."""
    assert _gaps(_ac("- [ ] [non-codebase] the fix is deployed to prod and the gate passes")) == []


def test_untagged_operational_ac_is_still_flagged() -> None:
    """The lint still fires on an untagged operational AC — widening the tag must not silence
    the coaching it exists to give."""
    assert _gaps(_ac("- [ ] the fix is deployed to prod and the gate passes"))
