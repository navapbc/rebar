"""Story e7e0: completion-verification lossless PASS capture (per-criterion record).

Completion verification emits findings ONLY for FAILING criteria, and a PASS previously left
no per-criterion record anywhere. This adds a positive `criteria[]` channel (one
{criterion, met, citation, kind} record per evaluated criterion) that rides ALONGSIDE the
failures-only `findings` invariant, plus a completion sidecar emitted on PASS (not only FAIL).
"""

from __future__ import annotations

import pytest

from rebar.llm import completion_sidecar
from rebar.llm.completion import reconcile_verdict

pytestmark = pytest.mark.unit


def _criteria() -> list[dict]:
    return [
        {
            "criterion": "AC1: the widget renders",
            "met": True,
            "citation": {"kind": "source", "description": "src/w.py:10-20"},
            "kind": "codebase-verifiable",
        },
        {
            "criterion": "AC2: deployed to prod",
            "met": True,
            "citation": {"kind": "source", "description": "comment: deploy #42"},
            "kind": "operator-attested",
        },
    ]


# ── HAPPY PATH (shared with the implementer) ────────────────────────────────────────────
def test_reconcile_pass_with_criteria_stays_pass() -> None:
    """A PASS verdict that carries a populated positive `criteria[]` (and empty `findings`) is
    NOT flipped to FAIL — the PASS-with-findings flip keys on `findings`, so the positive channel
    is preserved and `findings` stays empty."""
    v = {"verdict": "PASS", "findings": [], "criteria": _criteria()}
    reconcile_verdict(v)
    assert v["verdict"] == "PASS"
    assert v["findings"] == []
    assert len(v["criteria"]) == 2  # the positive per-criterion array survives untouched


def test_completion_verdict_model_roundtrips_criteria() -> None:
    """The load-bearing contract: the ``CompletionVerdict`` pydantic model carries ``criteria[]``
    as a DECLARED field, so the agent's positive array survives parsing instead of being dropped
    as an unmodeled extra (pydantic v2 ignores extras by default)."""
    from rebar.llm import contracts

    model = contracts.completion_verdict_response_model()
    parsed = model.model_validate({"verdict": "PASS", "findings": [], "criteria": _criteria()})
    dumped = parsed.model_dump()
    assert [c["criterion"] for c in dumped["criteria"]] == [
        "AC1: the widget renders",
        "AC2: deployed to prod",
    ]
    assert dumped["criteria"][0]["met"] is True
    assert dumped["criteria"][0]["kind"] == "codebase-verifiable"


def test_build_payload_pass_carries_criteria() -> None:
    """build_payload for a PASS verdict tags a PASS schema and persists the positive per-criterion
    array plus verdict/runner/model/material — the durable PASS-path audit record."""
    v = {
        "verdict": "PASS",
        "ticket_id": "T-1",
        "findings": [],
        "criteria": _criteria(),
        "runner": "fake",
        "model": "m",
    }
    p = completion_sidecar.build_payload(v, material="fp")
    assert p["schema"] == "completion_verifier_pass_v1"
    assert p["verdict"] == "PASS"
    assert p["findings"] == []
    assert [c["criterion"] for c in p["criteria"]] == [
        "AC1: the widget renders",
        "AC2: deployed to prod",
    ]
    assert p["material_fingerprint"] == "fp"
    assert p["runner"] == "fake" and p["model"] == "m"


# ── HELD-OUT ORACLE (restored) ──
def test_reconcile_fail_still_flips_even_with_criteria() -> None:
    """A verdict that lists FAILURE findings still flips to FAIL even when a positive criteria[]
    is also present — the failures-only findings invariant is authoritative for the verdict; the
    positive channel never rescues a listed failure."""
    v = {
        "verdict": "PASS",
        "findings": [{"criterion": "AC3", "detail": "missing", "severity": "high"}],
        "criteria": _criteria(),
    }
    reconcile_verdict(v)
    assert v["verdict"] == "FAIL"
    assert len(v["findings"]) == 1
    assert v.get("remediation")  # FAIL carries remediation guidance


def test_build_payload_fail_unchanged_schema_and_no_criteria_confusion() -> None:
    """A FAIL verdict still tags the FAIL schema and carries its findings; the criteria[] field
    (if absent) does not disturb the FAIL record shape (back-compat)."""
    v = {
        "verdict": "FAIL",
        "ticket_id": "T-2",
        "findings": [{"criterion": "AC1", "detail": "missing", "severity": "high"}],
        "runner": "fake",
        "model": "m",
    }
    p = completion_sidecar.build_payload(v, material=None)
    assert p["schema"] == "completion_verifier_fail_v1"
    assert p["verdict"] == "FAIL"
    assert p["findings"][0]["criterion"] == "AC1"
