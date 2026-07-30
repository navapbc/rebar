"""Unit tests for the blocking clarity-floor DET checks (ticket 49b8).

Two SEPARATE blocking DET checks (P6 stays advisory and monolithic):

* **P10 verification-presence** — a leaf plan must carry a ``## Testing`` /
  ``## Verification`` H2 section OR >=1 AC checklist item with an inline code
  span or a verification-vocabulary match (the exhaustive regex in
  ``det_clarity``).
* **P11 AC vagueness** — the boundary-fixed vague lexicon (both word
  boundaries; ``clean`` dropped; ``etc.`` kept with the code-span-proximity
  exemption measured on ORIGINAL-line span positions) scanned over AC item
  lines only.

The ten false-positive fixture lines are transcribed from the 304-passed-plan
backtest (each measured 0 FPs under the fixed rule) and MUST NOT fire; the
three positive cases MUST fire.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from rebar.llm.plan_review import registry
from rebar.llm.plan_review.det_clarity import (
    _VAGUE_LEXICON_FIXED,
    p10_verification_presence,
    p11_ac_vagueness,
    vague_hits_in_line,
)
from rebar.llm.plan_review.det_floor import DET_CHECKS, PlanContext, p6_ac_quality

_REPO_ROOT = Path(__file__).resolve().parents[2]


def _ctx(description: str, children: list[dict] | None = None) -> PlanContext:
    return PlanContext(
        ticket_id="clarity-det-test",
        ticket_type="task",
        title="Clarity DET probe",
        description=description,
        children=children or [],
    )


def _ac(*lines: str) -> str:
    return "## Acceptance Criteria\n" + "\n".join(lines) + "\n"


# ── P10 verification-presence ──────────────────────────────────────────────────
def test_p10_blocks_leaf_with_no_verification_signal() -> None:
    """No code-span AC, no vocabulary-matching AC, no Testing/Verification H2 → BLOCK."""
    r = p10_verification_presence(_ctx(_ac("- [ ] the feature works for users")))
    assert r.status == "fail"
    assert r.blocking is True
    assert r.blocked
    assert r.finding is not None and r.finding["evidence"]


def test_p10_passes_on_testing_section() -> None:
    desc = "## Testing\nRun the focused suite.\n\n" + _ac("- [ ] the feature works for users")
    r = p10_verification_presence(_ctx(desc))
    assert r.status == "pass" and not r.blocked


def test_p10_passes_on_verification_section() -> None:
    desc = "## Verification\nRun the api module.\n\n" + _ac("- [ ] the endpoint responds")
    assert p10_verification_presence(_ctx(desc)).status == "pass"


def test_p10_passes_on_backticked_command_ac() -> None:
    r = p10_verification_presence(
        _ctx(_ac("- [ ] the regression suite passes: `pytest tests/unit/test_x.py -q`"))
    )
    assert r.status == "pass"
    assert r.coverage["qualifying_ac_items"] == 1


def test_p10_passes_on_checked_ac() -> None:
    r = p10_verification_presence(
        _ctx(_ac("- [ ] the doc section is present, checked:manual read"))
    )
    assert r.status == "pass"


def test_p10_passes_on_vocabulary_ac() -> None:
    r = p10_verification_presence(_ctx(_ac("- [x] the output is verified against the fixture")))
    assert r.status == "pass"


def test_p10_container_is_a_natural_pass() -> None:
    r = p10_verification_presence(
        _ctx(_ac("- [ ] children deliver the feature"), children=[{"ticket_id": "c1"}])
    )
    assert r.status == "pass"
    assert r.coverage.get("container") is True


# ── P11 AC vagueness: the ten backtest FP lines must NOT fire ──────────────────
_FP_LINES = [
    "`make lint` exits 0 (ruff + format check clean)",
    "mypy --strict runs clean on the new module",
    "the parser clean parses all fixture files",
    "Clean preferred-schema responses round-trip",
    "the close precheck sees a clean tickets-tracker HEAD",
    "the core-wheel lane is clean after the split",
    "the collector cleanly collects all retired events",
    "stale worktrees are cleaned up by the janitor",
    "run `git grep -n 'pattern' docs/ src/`, etc. and confirm no hits",
    "Robustness: the simulated-stall test passes",
]


@pytest.mark.parametrize("line", _FP_LINES)
def test_p11_zero_matches_on_backtest_fp_lines(line: str) -> None:
    item = f"- [ ] {line}"
    assert vague_hits_in_line(item) == []
    r = p11_ac_vagueness(_ctx(_ac(item)))
    assert r.status == "pass" and not r.blocked


# ── P11 positives: must fire, and fire BLOCKING ────────────────────────────────
_POSITIVE_LINES = [
    "works properly",
    "handles errors, etc.",
    "reasonable performance",
]


@pytest.mark.parametrize("line", _POSITIVE_LINES)
def test_p11_fires_on_positive_lines(line: str) -> None:
    item = f"- [ ] {line}"
    assert vague_hits_in_line(item)
    r = p11_ac_vagueness(_ctx(_ac(item)))
    assert r.status == "fail"
    assert r.blocking is True
    assert r.blocked
    assert r.finding is not None and r.finding["evidence"]


def test_clean_absent_from_blocking_lexicon() -> None:
    assert "clean" not in _VAGUE_LEXICON_FIXED
    assert "etc." in _VAGUE_LEXICON_FIXED


def test_lexicon_requires_both_word_boundaries() -> None:
    # prefix-only matching (the old bug) would fire "robust" on "Robustness".
    assert vague_hits_in_line("- [ ] Robustness: the stall test passes") == []
    assert vague_hits_in_line("- [ ] the retry loop is robust") == ["robust"]


# ── the etc. code-span-proximity exemption (original-line positions) ───────────
def test_etc_within_30_chars_after_span_end_is_exempt() -> None:
    assert vague_hits_in_line("- [ ] run `git grep -n pat`, etc. and confirm no hits") == []


def test_etc_beyond_30_chars_after_span_end_fires() -> None:
    line = "- [ ] run `git grep -n pat` and then review every one of the produced hits, etc."
    assert vague_hits_in_line(line) == ["etc."]


def test_lexicon_hit_inside_code_span_never_fires() -> None:
    assert vague_hits_in_line("- [ ] the parser accepts literal `etc.` tokens as input") == []
    assert vague_hits_in_line("- [ ] render the `properly` keyword verbatim") == []


def test_p11_scans_ac_item_lines_only() -> None:
    """Vague prose OUTSIDE the AC checklist never blocks (P11 is AC-scoped)."""
    desc = "## Context\nThe old flow worked properly, etc.\n\n" + _ac(
        "- [ ] the endpoint returns HTTP 404 for a missing id"
    )
    assert p11_ac_vagueness(_ctx(desc)).status == "pass"


# ── P6 stays advisory and shares the fixed matching ────────────────────────────
def test_p6_advisory_uses_the_fixed_lexicon() -> None:
    ctx = _ctx(_ac("- [ ] the collector cleanly collects events; proof: `pytest -q`"))
    r = p6_ac_quality(ctx)
    assert r.blocking is False
    vague = [e for e in (r.finding or {}).get("evidence", []) if "vague/subjective" in e]
    assert not vague  # "cleanly" no longer matches (clean dropped + both boundaries)
    ctx2 = _ctx(_ac("- [ ] works properly; proof: `pytest -q`"))
    r2 = p6_ac_quality(ctx2)
    assert r2.blocking is False
    assert any("properly" in e for e in (r2.finding or {}).get("evidence", []))


# ── floor shape + docs ─────────────────────────────────────────────────────────
def test_det_checks_are_p1_to_p11() -> None:
    assert len(DET_CHECKS) == 11
    assert [c.__name__ for c in DET_CHECKS[-2:]] == [
        "p10_verification_presence",
        "p11_ac_vagueness",
    ]
    assert list(registry.CANONICAL_DET) == [f"P{i}" for i in range(1, 12)]


def test_gate_doc_documents_p10_and_p11() -> None:
    doc = (_REPO_ROOT / "docs" / "plan-review-gate.md").read_text(encoding="utf-8")
    assert "P10" in doc and "P11" in doc
    assert "verification-presence" in doc
    assert "vague" in doc.lower()
