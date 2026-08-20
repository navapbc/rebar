"""HELD-OUT edge oracle for the S4 prompt cutover (story f371, ADR 0101).

Withheld from the implementation subagent. Pins the three couplings that make this a
single-commit change: the verbatim quote in the gate doc, BOTH tag-bearing rules in the
packaged registry, and the digest/selector mechanism that guards the shipped contract text.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
REGISTRY = REPO / "src/rebar/llm/plan_review/criteria_routing.json"


def test_gate_doc_quotes_the_live_coach_template_verbatim() -> None:
    """`docs/plan-review-gate.md`'s move table reproduces move 14's template. If the template
    changes and the table does not, the doc misquotes coaching the author will actually see."""
    from rebar.llm.plan_review.coach_moves import MOVE_REGISTRY

    template = MOVE_REGISTRY["14"]["template"]
    # The doc renders `{subject}` filled in, so compare the stable head and tail fragments.
    head = template.split("{subject}")[0].strip()
    tail = template.split("{subject}")[-1].strip()
    doc = (REPO / "docs/plan-review-gate.md").read_text()
    assert head in doc, f"gate doc lost the move-14 template head: {head!r}"
    assert tail in doc, f"gate doc lost the move-14 template tail: {tail!r}"


def test_both_registry_rules_recognize_the_canonical_tag() -> None:
    """The packaged registry carries TWO tag-bearing rules and BOTH must be widened:
    `acceptance_items_classified` (the classification vocabulary) and
    `evidence_kind_matches_tag`, which FIRES on an untagged external outcome — left stale it
    would flag a correctly `[non-codebase]`-tagged criterion as untagged."""
    blob = REGISTRY.read_text()
    for rule in ("acceptance_items_classified", "evidence_kind_matches_tag"):
        assert rule in blob, f"{rule} missing from the registry"
    data = json.loads(blob)

    def _checks(obj, acc):
        if isinstance(obj, dict):
            for k, v in obj.items():
                if k == "check" and isinstance(v, str):
                    acc.append(v)
                else:
                    _checks(v, acc)
        elif isinstance(obj, list):
            for v in obj:
                _checks(v, acc)
        return acc

    tag_checks = [c for c in _checks(data, []) if "attested" in c or "non-codebase" in c]
    assert tag_checks, "no tag-bearing checklist rule found"
    for c in tag_checks:
        assert "non-codebase" in c, f"registry rule still names only the legacy tag: {c[:120]!r}"


@pytest.mark.parametrize(
    "path",
    [
        "src/rebar/llm/reviewers/completion_verifier.md",
        "src/rebar/llm/reviewers/plan_review_evidence_kind.md",
    ],
)
def test_recognition_prompts_teach_both_spellings(path: str) -> None:
    """Recognition-side prompts classify text the author already wrote, which may use either
    spelling. Teaching only one makes the LLM disagree with the deterministic matcher."""
    body = (REPO / path).read_text()
    assert "[non-codebase]" in body
    # A bare `operator-attested` substring is NOT sufficient evidence: these prompts also use
    # `operator-attested` as the name of the evidence KIND (the wire value), which is a
    # different thing from the legacy TAG. Pin the sentence that actually grants acceptance.
    assert "operator-attested" in body, "the legacy spelling vanished entirely"
    assert re.search(r"legacy", body) and re.search(r"still[- ]accepted", body), (
        "the prompt must STATE that the legacy tag is still accepted, not merely mention "
        "`operator-attested` as the name of the evidence kind"
    )


def test_contract_digests_were_deliberately_recomputed() -> None:
    """The digest pin is a DELIBERATE-UPDATE gate, not an obstacle: after the cutover the
    recorded digests and line counts must match the shipped files exactly. A stale pin means
    the contract text moved without anyone re-approving it."""
    from tests.unit.test_measurement_provenance_criterion import (
        CONTRACT_DIGESTS,
        _contract_lines,
    )

    for path, (expected_digest, expected_count) in CONTRACT_DIGESTS.items():
        lines = _contract_lines((REPO / path).read_text())
        assert len(lines) == expected_count, (
            f"{path}: pinned count {expected_count} but found {len(lines)}"
        )
        actual = hashlib.sha256("\n".join(lines).encode()).hexdigest()[:16]
        assert actual == expected_digest, f"{path}: digest {actual} != pinned {expected_digest}"


def test_contract_selector_still_matches_the_cutover_text() -> None:
    """THE SELECTOR TRAP. `_contract_lines` keys on the OLD tokens. If the prompts cut over
    and the selector does not, it matches ZERO lines and the digest test fails on a
    misleading count assertion instead of a real contract change."""
    from tests.unit.test_measurement_provenance_criterion import _contract_lines

    for path in (
        "src/rebar/llm/reviewers/plan_review_F1.md",
        "src/rebar/llm/reviewers/plan_review_E2.md",
        "src/rebar/llm/reviewers/plan_review_E6.md",
        "src/rebar/llm/plan_review/coach_moves.py",
    ):
        assert _contract_lines((REPO / path).read_text()), (
            f"{path}: selector matched NO contract lines after cutover"
        )
