"""HELD-OUT edge oracle for the audit page's `kind` widening (story a356, ADR 0101).

Withheld from the implementation subagent. Pins the narrowness of `lacking` — it must flag
ONLY an unmet out-of-codebase criterion — and the default/unknown-value behavior that keeps
the reader fail-safe.
"""

from __future__ import annotations

import pytest

from rebar.audit.page import _completion_section


def _row(**criterion) -> dict:
    base = {"criterion": "c", "met": False}
    base.update(criterion)
    return _completion_section({"sidecar": {"verdict": "PASS", "criteria": [base]}})["criteria"][0]


@pytest.mark.parametrize("kind", ["non-codebase", "operator-attested"])
def test_met_criterion_is_never_lacking(kind: str) -> None:
    """`lacking` means 'attestation missing', so a MET criterion is never lacking under
    either spelling — widening the accepted set must not widen the flag."""
    assert _row(kind=kind, met=True)["lacking"] is False


@pytest.mark.parametrize(
    "kind",
    ["codebase-verifiable", "non_codebase", "noncodebase", "NON-CODEBASE", "", "mixed"],
)
def test_only_the_two_canonical_values_are_lacking(kind: str) -> None:
    """The reader stays EXACT on the two accepted values. An unknown or near-miss `kind`
    falls back to not-lacking rather than guessing — the same fail-safe direction ADR 0043
    chose for the tag, so a garbled record cannot manufacture an attestation gap.

    Note this is deliberately case-SENSITIVE: unlike the author-typed AC tag, `kind` is a
    machine-emitted wire value, so there is no human typo to forgive.
    """
    assert _row(kind=kind)["lacking"] is False


@pytest.mark.parametrize("kind", ["non-codebase", "operator-attested", "codebase-verifiable"])
def test_kind_is_passed_through_verbatim(kind: str) -> None:
    """The row still reports the kind it was given — the reader classifies, it does not
    rewrite history, so a legacy record keeps displaying its own value."""
    assert _row(kind=kind)["kind"] == kind


def test_absent_kind_defaults_to_codebase_verifiable() -> None:
    """A record with no `kind` at all keeps defaulting to the codebase-verifiable bar."""
    row = _row()
    assert row["kind"] == "codebase-verifiable"
    assert row["lacking"] is False


def test_mixed_criteria_are_flagged_independently() -> None:
    """A verdict mixing both spellings and a codebase criterion flags exactly the two
    out-of-codebase rows — proving the widening is per-row, not a global switch."""
    section = _completion_section(
        {
            "sidecar": {
                "verdict": "PASS",
                "criteria": [
                    {"criterion": "a", "met": False, "kind": "non-codebase"},
                    {"criterion": "b", "met": False, "kind": "operator-attested"},
                    {"criterion": "c", "met": False, "kind": "codebase-verifiable"},
                    {"criterion": "d", "met": True, "kind": "non-codebase"},
                ],
            }
        }
    )
    assert [r["lacking"] for r in section["criteria"]] == [True, True, False, False]


def test_fail_verdict_path_is_untouched() -> None:
    """The widening lives in the PASS branch only; a FAIL verdict still surfaces findings."""
    section = _completion_section(
        {"sidecar": {"verdict": "FAIL", "findings": [{"criterion": "x", "summary": "s"}]}}
    )
    assert section["is_pass"] is False
    assert section["findings"] == [{"criterion": "x", "summary": "s"}]
