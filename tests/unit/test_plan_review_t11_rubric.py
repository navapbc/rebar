"""Contract tests for the T11 migration rubric's post-rollback applicability rule.

The POST-ROLLBACK STATE paragraph must be gated behind a four-condition
applicability rule (portable across store technologies) plus an explicit
anti-false-positive line for purely additive, backward-compatible changes —
not the old unconditional "must assert post-rollback state" wording that
generated tautological findings against append-only stores.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
RUBRIC_PATH = ROOT / "src/rebar/llm/reviewers/plan_review_T11.md"


def _rubric_text() -> str:
    return RUBRIC_PATH.read_text()


def _post_rollback_paragraph() -> str:
    text = _rubric_text()
    match = re.search(r"POST-ROLLBACK[^\n]*(?:\n(?!\n)[^\n]*)*", text)
    assert match, "POST-ROLLBACK paragraph missing from T11 rubric"
    return match.group(0)


def test_four_applicability_conditions_are_present() -> None:
    paragraph = _post_rollback_paragraph()
    assert re.search(r"\(a\).*interpret", paragraph, re.IGNORECASE | re.DOTALL)
    assert re.search(r"\(b\).*(rewrites|migrates).*in place", paragraph, re.IGNORECASE | re.DOTALL)
    assert re.search(r"\(c\).*(removes|narrows).*read path", paragraph, re.IGNORECASE | re.DOTALL)
    assert re.search(
        r"\(d\).*one-way.*(no back-out|back-out)", paragraph, re.IGNORECASE | re.DOTALL
    )


def test_findings_raised_only_under_conditions() -> None:
    paragraph = _post_rollback_paragraph()
    assert re.search(r"\bONLY\b", paragraph), (
        "post-rollback findings must be restricted (ONLY) to the four conditions"
    )


def test_anti_false_positive_line_present() -> None:
    paragraph = _post_rollback_paragraph()
    assert re.search(
        r"(purely )?additive.*backward-compatible", paragraph, re.IGNORECASE | re.DOTALL
    )
    assert re.search(r"residual data after rollback is the norm", paragraph, re.IGNORECASE)


def test_old_unconditional_wording_is_gone() -> None:
    text = _rubric_text()
    assert "must assert on the schema STATE AFTER rollback" not in text
    assert "not merely that 'rollback exits 0'" not in text


def test_applicability_rule_names_no_store_technology() -> None:
    """The rule itself is portable — no store technology named.

    The illustrative anti-FP examples (after the anti-FP marker) are exempt.
    """
    paragraph = _post_rollback_paragraph()
    rule_part = re.split(r"ANTI-FP", paragraph, maxsplit=1)[0]
    banned = re.compile(
        r"\b(SQL|Postgres|PostgreSQL|MySQL|SQLite|Mongo\w*|DynamoDB|Kafka|"
        r"event[- ]store|document database|git|JSONL?)\b",
        re.IGNORECASE,
    )
    match = banned.search(rule_part)
    assert match is None, f"store technology named in applicability rule: {match.group(0)!r}"
