"""HELD-OUT edge oracle for the `kind` wire-value cutover (story b320, ADR 0101).

Withheld from the implementation subagent. The risk here is NOT that the new value fails to
emit — it is that the ~827 tickets in the live store holding `kind: "operator-attested"` in
immutable COMPLETION_VERDICT events stop rendering, silently, because `kind` is an
unconstrained schema string with no enum and nothing validates a mismatch.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from rebar.audit.page import _completion_section

REPO = Path(__file__).resolve().parents[2]


def _row(kind: str, met: bool = False) -> dict:
    return _completion_section(
        {"sidecar": {"verdict": "PASS", "criteria": [{"criterion": "c", "met": met, "kind": kind}]}}
    )["criteria"][0]


@pytest.mark.parametrize("kind", ["non-codebase", "operator-attested"])
def test_reader_still_accepts_both_values_after_the_emitter_moved(kind: str) -> None:
    """THE POINT OF THE ORDERING. The reader landed first precisely so legacy persisted
    verdicts keep rendering once emission switches. Regressing the reader here would break
    every historical ticket without any test or schema failing."""
    assert _row(kind)["lacking"] is True


def test_codebase_verifiable_is_still_not_lacking() -> None:
    """The widening stays narrow — `lacking` flags a missing attestation, not any unmet item."""
    assert _row("codebase-verifiable")["lacking"] is False


def test_contracts_and_schema_agree_on_the_value_pair() -> None:
    """Two independent declarations of the same wire contract; drift between them is invisible
    at runtime because `kind` has no enum, so pin their agreement explicitly."""
    contracts = (REPO / "src/rebar/llm/contracts.py").read_text()
    schema = json.loads((REPO / "src/rebar/schemas/completion_verdict.schema.json").read_text())
    desc = schema["properties"]["criteria"]["items"]["properties"]["kind"]["description"]
    for token in ("codebase-verifiable", "non-codebase"):
        assert token in contracts, f"contracts.py lost {token}"
        assert token in desc, f"schema lost {token}"
    assert "operator-attested" not in desc, "schema should describe the CANONICAL pair only"


def test_verifier_prompt_still_teaches_legacy_tag_recognition() -> None:
    """Cutting the emitted KIND over must not remove the prompt's statement that the legacy
    TAG spelling is still accepted — the tag and the wire value are different surfaces, and
    conflating them would silently drop compatibility for authors."""
    body = (REPO / "src/rebar/llm/reviewers/completion_verifier.md").read_text()
    assert "operator-attested" in body, "legacy tag recognition vanished"
    assert "still accepted" in body or "still-accepted" in body


def test_registry_classification_vocabulary_matches_the_emitted_value() -> None:
    """`acceptance_items_classified` tells the model the vocabulary to classify into. If it
    still says `operator-attested` while the verifier emits `non-codebase`, the two LLM-facing
    contracts disagree and the mismatch is unobservable at runtime."""
    blob = (REPO / "src/rebar/llm/plan_review/criteria_routing.json").read_text()
    data = json.loads(blob)

    def _checks(o, acc):
        if isinstance(o, dict):
            for k, v in o.items():
                acc.append(v) if k == "check" and isinstance(v, str) else _checks(v, acc)
        elif isinstance(o, list):
            for v in o:
                _checks(v, acc)
        return acc

    classify = [c for c in _checks(data, []) if "Classify the completion evidence" in c]
    assert classify, "acceptance_items_classified check not found"
    assert all("non-codebase" in c for c in classify)
