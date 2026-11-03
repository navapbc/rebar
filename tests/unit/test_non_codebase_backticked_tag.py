"""A backticked ``[non-codebase]`` tag counts exactly like the bare tag (bug 3d6f-1f34).

Writing the tag in a code span at the start of the checkbox text is the ordinary markdown
instinct and the form rebar's own guidance rendered, yet every deterministic parser rejected
it: the close precheck refused the item as unchecked, and a TICKED backticked item was handled
as an ordinary criterion, so its ``provenance:`` requirement and the laundering check were
skipped silently. These tests pin that every parser of the tag accepts both forms, for both
spellings, and that the start-only rule and the near-miss rule are unchanged.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from rebar._commands import txn
from rebar._commands._seam import CommandError
from rebar.llm.completion_reconcile import COMPLETION_REMEDIATION_GUIDANCE
from rebar.llm.plan_review import det_attestation_launder, det_measurement_provenance
from rebar.llm.plan_review.decide_ops import operator_attested_ac_texts
from rebar.llm.plan_review.det_operator_attested import (
    _OPERATOR_ATTESTED_TAG_RE,
    misplaced_tag_issues,
    misplaced_tag_lines,
    operator_evidence_ac_gaps,
)

pytestmark = pytest.mark.unit

_TID = "3d6f-1f34-1a7c-4dd1"
_TEXT = "The operator runs terraform apply and records the live Bedrock call."
_PROVENANCE = (
    "      provenance: environment=production; principal=release-operator; "
    "privilege_posture=production-equivalent; instrument=live-call — console shows green"
)
_SPELLINGS = ("non-codebase", "operator-attested", "NON-CODEBASE")
_BOXES = ("[ ]", "[x]", "[X]")


def _bare(box: str, token: str) -> str:
    return f"- {box} [{token}] {_TEXT}"


def _backticked(box: str, token: str) -> str:
    return f"- {box} `[{token}]` {_TEXT}"


def _ac(*items: str) -> str:
    return "## Acceptance Criteria\n" + "\n".join(items) + "\n"


def _install_state(monkeypatch, description: str) -> None:
    monkeypatch.setattr(
        txn, "reduce_ticket", lambda _path: {"status": "in_progress", "description": description}
    )


# ── the shared matcher ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("box", _BOXES)
@pytest.mark.parametrize("token", _SPELLINGS)
def test_backticked_and_bare_tag_resolve_the_same(box: str, token: str) -> None:
    bare = _OPERATOR_ATTESTED_TAG_RE.match(_bare(box, token))
    ticked = _OPERATOR_ATTESTED_TAG_RE.match(_backticked(box, token))
    assert bare is not None
    assert ticked is not None
    assert ticked.group(1) == bare.group(1) == _TEXT


@pytest.mark.parametrize(
    "line",
    [
        f"- [ ] `[non-codebase] {_TEXT}",  # unbalanced: opening backtick only
        f"- [ ] `[non-codebase]] {_TEXT}",  # unbalanced: no closing backtick
        f"- [ ] ``[non-codebase]`` {_TEXT}",  # double-backtick span is not the accepted form
        f"- [ ] {_TEXT} `[non-codebase]`",  # a later code-span mention is not a tag
        f"- [ ] The guide documents the `[non-codebase]` tag. {_TEXT}",
        f"- [ ] `[non_codebase]` {_TEXT}",  # near-miss token
        f"- [ ] `[operator_attested]` {_TEXT}",
        "- [ ] `[non-codebase]`",  # no criterion text
    ],
)
def test_non_tags_stay_unmatched(line: str) -> None:
    assert _OPERATOR_ATTESTED_TAG_RE.match(line) is None


def test_decide_ops_returns_the_same_text_for_both_forms() -> None:
    description = _ac(_bare("[ ]", "non-codebase"), _backticked("[x]", "operator-attested"))
    assert operator_attested_ac_texts(description) == [_TEXT, _TEXT]


# ── the close precheck ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("token", _SPELLINGS)
def test_unchecked_backticked_item_is_exempt_from_the_close_precheck(token: str) -> None:
    state = {"description": _ac("- [x] Done.", _backticked("[ ]", token))}
    assert txn.ensure_ac_boxes_checked(_TID, "/nonexistent", ticket_state=state) is None


def test_untagged_unchecked_item_is_still_refused_with_the_exact_form() -> None:
    state = {"description": _ac(_backticked("[ ]", "non-codebase"), f"- [ ] {_TEXT}")}
    with pytest.raises(CommandError) as ei:
        txn.ensure_ac_boxes_checked(_TID, "/nonexistent", ticket_state=state)
    msg = str(ei.value)
    assert f"  - [ ] {_TEXT}" in msg
    assert f"`[non-codebase]` {_TEXT}" not in msg  # the tagged item is not an offender
    assert "- [ ] [non-codebase] <criterion>" in msg  # the exact accepted form is named


# ── a ticked backticked box is an ATTESTED criterion ─────────────────────────────


def test_ticked_backticked_item_without_provenance_is_a_provenance_gap() -> None:
    description = _ac(_backticked("[x]", "non-codebase"))
    gaps = det_measurement_provenance.provenance_gaps(description)
    assert [line for line, _ in gaps] == [_backticked("[x]", "non-codebase")]


def test_ticked_backticked_item_with_provenance_has_no_gap() -> None:
    description = _ac(_backticked("[x]", "non-codebase"), _PROVENANCE)
    assert det_measurement_provenance.provenance_gaps(description) == []


def test_ticked_backticked_item_citing_a_test_is_laundering() -> None:
    line = "- [x] `[non-codebase]` scoping holds; proxy: tests/unit/test_scan_scoping.py"
    gaps = det_attestation_launder.laundering_gaps(_ac(line, _PROVENANCE))
    assert [ln for ln, _ in gaps] == [line]


def test_close_refuses_a_ticked_backticked_item_without_provenance(monkeypatch) -> None:
    _install_state(monkeypatch, _ac(_backticked("[x]", "non-codebase")))
    with pytest.raises(CommandError) as ei:
        txn.ensure_attested_items_valid(_TID, "/nonexistent")
    assert "provenance:" in str(ei.value)
    assert ei.value.returncode == 1


# ── plan-review DET checks ────────────────────────────────────────────────────────


def test_backticked_item_counts_as_tagged_for_the_operator_evidence_lint() -> None:
    untagged = f"- [ ] {_TEXT}"
    assert operator_evidence_ac_gaps([untagged])  # the fixture really carries a marker
    assert operator_evidence_ac_gaps([_backticked("[ ]", "non-codebase")]) == []


def test_backticked_tag_at_the_start_is_not_misplaced() -> None:
    mid = f"- [ ] AC4 [non-codebase]: {_TEXT}"
    lines = [_backticked("[ ]", "non-codebase"), _backticked("[x]", "operator-attested"), mid]
    assert misplaced_tag_lines(lines) == [mid]


# ── every guidance example is accepted ────────────────────────────────────────────

_EXAMPLE_RE = re.compile(
    r"- \[[ xX]?\] `?\[(?:non-codebase|operator-attested)\]`?[^`\n()\"]*", re.IGNORECASE
)
_GUIDE = Path(__file__).resolve().parents[2] / "src/rebar/_guides/writing-a-passing-plan.md"


def _close_refusal_message() -> str:
    state = {"description": _ac(f"- [ ] AC4 [non-codebase]: {_TEXT}")}
    with pytest.raises(CommandError) as ei:
        txn.ensure_ac_boxes_checked(_TID, "/nonexistent", ticket_state=state)
    return str(ei.value)


def _guidance_surfaces() -> dict[str, str]:
    return {
        "completion_reconcile": COMPLETION_REMEDIATION_GUIDANCE,
        "misplaced_tag_issues": " ".join(misplaced_tag_issues(["- [ ] AC4 [non-codebase]: x"])),
        "txn_close_refusal": _close_refusal_message(),
        "writing_a_passing_plan": _GUIDE.read_text(encoding="utf-8"),
    }


@pytest.mark.parametrize("surface", sorted(_guidance_surfaces()))
def test_every_guidance_example_is_accepted(surface: str) -> None:
    text = _guidance_surfaces()[surface]
    examples = [m.group(0).strip() for m in _EXAMPLE_RE.finditer(text)]
    # Drop the refusal's own echo of the offending (misplaced) line; it is not an example.
    examples = [e for e in examples if "AC4" not in e]
    assert examples, f"{surface}: no tag example found; the extraction pattern is stale"
    for example in examples:
        assert _OPERATOR_ATTESTED_TAG_RE.match(example), f"{surface}: {example!r}"
