from __future__ import annotations

import re
from pathlib import Path

from rebar.llm.plan_review.det_operator_attested import operator_evidence_issues

ROOT = Path(__file__).resolve().parents[2]
INTERNAL_ADR = re.compile(r"\bADR(?:-|\s+)0043\b", re.IGNORECASE)


def test_operator_attested_coaching_is_self_contained() -> None:
    issues = operator_evidence_issues(
        ["- [ ] the service is deployed to production and the vote outcome is recorded"]
    )
    assert issues
    assert not INTERNAL_ADR.search("\n".join(issues))


def test_plan_review_prompts_do_not_expose_internal_adr_0043() -> None:
    prompt_paths = (
        ROOT / "src/rebar/llm/reviewers/plan_review_E2.md",
        ROOT / "src/rebar/llm/reviewers/plan_review_E6.md",
        ROOT / "src/rebar/llm/reviewers/plan_review_F1.md",
        ROOT / "src/rebar/llm/reviewers/plan_review_T13.md",
        ROOT / "src/rebar/llm/reviewers/plan_review_T14.md",
        ROOT / "src/rebar/llm/reviewers/plan_review_hedge.md",
    )
    leaked = [
        str(path.relative_to(ROOT))
        for path in prompt_paths
        if INTERNAL_ADR.search(path.read_text())
    ]
    assert leaked == []
