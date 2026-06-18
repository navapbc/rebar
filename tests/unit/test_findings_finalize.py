"""Unit tests for the WS-D1 finalization strategy (rebar.llm.findings).

finalize_findings consolidates build→resolve→validate; finalize_outcome adds the
mode dispatch (findings/structured/text). RunRequest gains defaulted output_schema/
mode without disturbing the review path.
"""

from __future__ import annotations

import pytest

from rebar.llm import findings as F
from rebar.llm.errors import StructuredOutputError


def test_finalize_findings_builds_valid_review_result() -> None:
    pytest.importorskip("jsonschema")
    result = F.finalize_findings(
        [{"severity": "high", "dimension": "bugs", "detail": "x"}],
        runner="fake",
        target={"ticket_ids": ["t1"]},
        reviewers=["r"],
    )
    assert result["runner"] == "fake"
    assert result["findings"][0]["severity"] == "high"


def test_finalize_outcome_findings_mode() -> None:
    pytest.importorskip("jsonschema")
    outcome = {"structured_response": {"findings": [], "summary": "all clear"}}
    result = F.finalize_outcome(outcome, mode="findings", runner="langgraph", model="m")
    assert result["summary"] == "all clear"
    assert result["findings"] == []


def test_finalize_outcome_text_mode() -> None:
    class _Msg:
        content = "the final answer"

    outcome = {"messages": [_Msg()], "structured_response": None}
    result = F.finalize_outcome(outcome, mode="text", runner="langgraph")
    assert result["text"] == "the final answer"
    assert result["runner"] == "langgraph"


def test_finalize_outcome_structured_no_schema_passthrough() -> None:
    outcome = {"structured_response": {"k": "v", "n": 1}}
    result = F.finalize_outcome(outcome, mode="structured", runner="langgraph")
    assert result["k"] == "v"
    assert result["n"] == 1
    assert result["runner"] == "langgraph"


def test_finalize_outcome_structured_validates_against_schema() -> None:
    pytest.importorskip("jsonschema")
    # gate_result requires {verdict, reason}; a payload missing them must fail.
    outcome = {"structured_response": {"foo": "bar"}}
    with pytest.raises(F.FindingsError, match="gate_result"):
        F.finalize_outcome(
            outcome, mode="structured", output_schema="gate_result", runner="langgraph"
        )


def test_finalize_outcome_structured_passes_valid_schema() -> None:
    pytest.importorskip("jsonschema")
    outcome = {"structured_response": {"verdict": "pass", "reason": "ok"}}
    result = F.finalize_outcome(
        outcome, mode="structured", output_schema="gate_result", runner="langgraph"
    )
    assert result["verdict"] == "pass"


def test_finalize_outcome_missing_structured_response_raises() -> None:
    with pytest.raises(StructuredOutputError, match="no structured"):
        F.finalize_outcome({"structured_response": None}, mode="findings", runner="langgraph")


def test_runrequest_defaults_are_backward_compatible() -> None:
    from rebar.llm.config import LLMConfig
    from rebar.llm.runner import RunRequest

    req = RunRequest(system_prompt="s", instructions="i", config=LLMConfig())
    assert req.mode == "findings"
    assert req.output_schema is None
