from __future__ import annotations

import re
from pathlib import Path

from rebar.llm.findings import resolve_citations
from rebar.llm.plan_review.det_operator_attested import operator_evidence_issues

ROOT = Path(__file__).resolve().parents[2]
INTERNAL_ADR = re.compile(r"\bADR(?:-|\s+)0043\b", re.IGNORECASE)


def test_operator_attested_coaching_is_self_contained() -> None:
    issues = operator_evidence_issues(
        ["- [ ] the service is deployed to production and the vote outcome is recorded"]
    )
    assert issues
    assert not INTERNAL_ADR.search("\n".join(issues))


def test_operator_attested_coaching_prescribes_canonical_tag() -> None:
    """P6 coaching for an untagged outside-world AC must teach the canonical [non-codebase]
    tag and must NOT prescribe the legacy [operator-attested] alias (ADR 0101: 'author-facing
    guidance teaches only [non-codebase]'). The compatibility matcher still accepts either
    spelling — that lives in test_non_codebase_tag.py — but the emitted advice is author-facing
    guidance and so may name only the canonical tag."""
    issues = operator_evidence_issues(
        ["- [ ] the service is deployed to production and the vote outcome is recorded"]
    )
    assert issues
    for issue in issues:
        assert "[non-codebase]" in issue
        assert "[operator-attested]" not in issue


def test_operator_attested_coaching_negative_control() -> None:
    """Contrast case: an AC already tagged with the canonical [non-codebase] declares its
    out-of-codebase evidence, so no coaching is emitted at all — proving the assertion above
    distinguishes the untagged (coached) case from the tagged (silent) one."""
    assert (
        operator_evidence_issues(
            ["- [ ] [non-codebase] the service is deployed to production and the vote is recorded"]
        )
        == []
    )


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


def test_client_adr_present_in_target_repo_remains_grounded(tmp_path: Path) -> None:
    client_adr = tmp_path / "docs/adr/0043-client-authored-decision.md"
    client_adr.parent.mkdir(parents=True)
    client_adr.write_text("# Client decision\n\nUse ticket-recorded deployment evidence.\n")
    result = {
        "findings": [
            {
                "citations": [
                    {
                        "kind": "file",
                        "path": "docs/adr/0043-client-authored-decision.md",
                        "line_start": 1,
                        "line_end": 3,
                    }
                ]
            }
        ]
    }

    resolve_citations(result, str(tmp_path))

    citation = result["findings"][0]["citations"][0]
    assert citation == {
        "kind": "file",
        "path": "docs/adr/0043-client-authored-decision.md",
        "line_start": 1,
        "line_end": 3,
    }
