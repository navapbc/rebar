"""Completion-verdict `kind` wire value cuts over to `non-codebase` (story b320, ADR 0101).

ADR 0101 requires the emitted `kind` to change "in lockstep" with the tag so the deterministic
matcher and the LLM's classification cannot diverge. The READER already accepts both values
(landed earlier), so this is the emitter half. Happy path.
"""

from __future__ import annotations

import json
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def test_contracts_declare_the_new_kind_value() -> None:
    """The pydantic Field description steers what the model emits."""
    body = (REPO / "src/rebar/llm/contracts.py").read_text()
    assert "codebase-verifiable | non-codebase" in body


def test_schema_declares_the_new_kind_value() -> None:
    """The published output schema documents the same two values as contracts.py."""
    schema = json.loads((REPO / "src/rebar/schemas/completion_verdict.schema.json").read_text())
    kind = schema["properties"]["criteria"]["items"]["properties"]["kind"]
    assert "non-codebase" in kind["description"]


def test_verifier_prompt_emits_the_new_kind_value() -> None:
    """The verifier's output-field instruction names the value the model must emit."""
    body = (REPO / "src/rebar/llm/reviewers/completion_verifier.md").read_text()
    assert "`codebase-verifiable` or `non-codebase`" in body
