"""The structured-output reliability stack (1268): deterministic tolerant parse
(json-repair), Pydantic validators with bounds, output-mode selection, stop_reason
handling, and a structured-output validity eval — all offline (no model call).
"""

from __future__ import annotations

import pytest
from pydantic import BaseModel, field_validator

from rebar.llm import structured
from rebar.llm.errors import StructuredOutputError


class _Verdict(BaseModel):
    verdict: str
    confidence: float = 1.0

    @field_validator("confidence")
    @classmethod
    def _bound(cls, v: float) -> float:
        # BOUNDS live in the validator (NOT the JSON Schema, to stay inside Anthropic's
        # strict-grammar subset) — they fire during validate_to.
        if not (0.0 <= v <= 1.0):
            raise ValueError("confidence must be in [0, 1]")
        return v


# ── Layer 2: deterministic tolerant parse (json-repair) ────────────────────────

_NEAR_MISS = {
    "strict": '{"verdict": "PASS"}',
    "markdown_fence": '```json\n{"verdict": "PASS"}\n```',
    "trailing_comma": '{"verdict": "PASS",}',
    "unclosed_brace": '{"verdict": "PASS"',
    "single_quotes": "{'verdict': 'PASS'}",
    "prose_wrapped": 'Sure! Here is the result: {"verdict": "PASS"} — hope that helps.',
}


@pytest.mark.parametrize("name", sorted(_NEAR_MISS))
def test_tolerant_parse_repairs_near_miss(name):
    parsed = structured.tolerant_parse(_NEAR_MISS[name])
    assert isinstance(parsed, dict) and parsed.get("verdict") == "PASS"


def test_tolerant_parse_first_object_wins_on_multi_object():
    # A draft-then-correction (two objects) must pick the FIRST deterministically —
    # NOT json-repair's last-wins surprise.
    parsed = structured.tolerant_parse('{"verdict": "PASS"} {"verdict": "FAIL"}')
    assert parsed == {"verdict": "PASS"}


def test_tolerant_parse_empty_is_error():
    with pytest.raises(StructuredOutputError, match="empty"):
        structured.tolerant_parse("   ")


def test_tolerant_parse_unparseable_is_error():
    with pytest.raises(StructuredOutputError):
        structured.tolerant_parse("the model declined entirely, no json here at all")


# ── Layer 3: validators (bounds in the validator, not the schema) ──────────────


def test_validate_to_accepts_valid():
    obj = structured.validate_to(_Verdict, {"verdict": "PASS", "confidence": 0.9})
    assert obj.verdict == "PASS" and obj.confidence == 0.9


def test_validate_to_enforces_validator_bounds():
    with pytest.raises(StructuredOutputError, match="confidence"):
        structured.validate_to(_Verdict, {"verdict": "PASS", "confidence": 1.5})


def test_validate_to_rejects_non_object():
    with pytest.raises(StructuredOutputError, match="object"):
        structured.validate_to(_Verdict, ["not", "a", "dict"])


def test_parse_structured_combines_layers():
    # A near-miss (fenced + trailing comma) that nonetheless yields a valid verdict.
    obj = structured.parse_structured(
        '```json\n{"verdict": "PASS", "confidence": 0.5,}\n```', _Verdict
    )
    assert obj.verdict == "PASS" and obj.confidence == 0.5


# ── Layer 1: output-mode selection ─────────────────────────────────────────────


def test_output_mode_native_for_enforcing_providers():
    pytest.importorskip("pydantic_ai")
    from pydantic_ai import NativeOutput, PromptedOutput

    assert isinstance(structured.output_mode(_Verdict, "openai:gpt-4o"), NativeOutput)
    assert isinstance(structured.output_mode(_Verdict, "google-gla:gemini-2.5-flash"), NativeOutput)
    # Anthropic (and unknown providers) -> PromptedOutput (safe, thinking-compatible).
    assert isinstance(structured.output_mode(_Verdict, "anthropic:claude-opus-4-8"), PromptedOutput)


def test_output_mode_forces_prompted_under_thinking():
    pytest.importorskip("pydantic_ai")
    from pydantic_ai import PromptedOutput

    # Even a native-capable provider must NOT use forced/native constraint with extended
    # thinking (the documented Anthropic 400 / provider incompatibility).
    assert isinstance(
        structured.output_mode(_Verdict, "openai:gpt-4o", thinking=True), PromptedOutput
    )


# ── stop_reason handling ───────────────────────────────────────────────────────


@pytest.mark.parametrize("reason", ["refusal", "max_tokens", "length", "content_filter", "error"])
def test_check_stop_reason_raises_on_bad(reason):
    # Both raw Anthropic stop reasons AND Pydantic AI normalized finish reasons.
    with pytest.raises(StructuredOutputError):
        structured.check_stop_reason(reason)


@pytest.mark.parametrize("reason", ["end_turn", "tool_use", "stop", "tool_call", None])
def test_check_stop_reason_passes_on_normal(reason):
    structured.check_stop_reason(reason)  # no raise


# ── Bounds in the REAL contract models (not just the toy) ──────────────────────


def test_real_completion_verdict_normalizes_garbled_to_fail():
    # M2: the production CompletionVerdict's validator (bounds in the validator, not the
    # schema) coerces a garbled/truncated verdict to FAIL — fail-safe, never silent PASS.
    from rebar.llm.contracts import completion_verdict_response_model

    Model = completion_verdict_response_model()
    assert Model(verdict="pass").verdict == "PASS"  # normalized
    assert Model(verdict="PA").verdict == "FAIL"  # garbled/truncated -> fail-safe
    assert Model(verdict="anything").verdict == "FAIL"


def test_real_finding_clamps_out_of_range_confidence():
    from rebar.llm.findings import finding_model

    Finding = finding_model()
    f = Finding(severity="high", dimension="x", detail="d", confidence=1.5)
    assert f.confidence == 1.0  # clamped into [0, 1] by the validator


# ── The structured-output validity eval (the gate) ─────────────────────────────


def test_structured_validity_eval_meets_threshold():
    # A corpus of realistic near-miss model outputs; the deterministic stack must
    # recover a valid structured verdict from >= 99% of them (the recall/false-accept
    # gate that authorizes retiring the second-interpreter LLM). Only genuinely
    # content-empty output (no recoverable JSON) is allowed to fail.
    corpus = list(_NEAR_MISS.values()) + [
        '{"verdict":"FAIL","confidence":0.2}',
        'Result:\n```\n{"verdict": "PASS"}\n```',
        '{"verdict": "PASS"  "confidence": 0.7}',  # missing comma
        '{\n  "verdict": "PASS",\n  "confidence": 1.0\n}',
    ]
    recovered = 0
    for text in corpus:
        try:
            structured.validate_to(_Verdict, structured.tolerant_parse(text))
            recovered += 1
        except StructuredOutputError:
            pass
    validity = recovered / len(corpus)
    assert validity >= 0.99, f"structured validity {validity:.2%} below the 99% gate"


def test_structured_validity_eval_false_accept_arm():
    # The other half of the gate (the AC says recall AND false-accept): output the stack
    # must NOT silently turn into a wrong-but-valid object. Each case must EITHER raise
    # OR recover the CORRECT value — never fabricate. Uses the REAL CompletionVerdict so
    # its fail-safe verdict validator participates.
    from rebar.llm.contracts import completion_verdict_response_model

    Model = completion_verdict_response_model()
    cases = [
        # (input, expected_verdict_or_None)  — None means "must raise, not fabricate".
        ('{"verdict":"PASS"} {"verdict":"FAIL"}', "PASS"),  # first-wins, not last
        ('{"verdict": "PA', "FAIL"),  # truncated -> json-repair -> fail-safe FAIL
        ("the model refused, no json", None),  # nothing recoverable -> must raise
    ]
    false_accepts = 0
    for text, expected in cases:
        try:
            obj = Model(**structured.tolerant_parse(text))
            if expected is None or obj.verdict != expected:
                false_accepts += 1  # fabricated a wrong/unexpected value
        except (StructuredOutputError, Exception):
            if expected is not None:
                false_accepts += 1  # should have recovered, didn't
    assert false_accepts == 0, f"{false_accepts} false-accept(s): garbage produced wrong output"


def test_second_interpreter_llm_is_not_used_by_pydantic_ai_runner():
    # The anti-pattern (a SECOND model call to interpret/extract output) must be gone
    # from the new runtime: the PydanticAIRunner uses the deterministic stack +
    # output_mode, never an extract-via-second-LLM step.
    import inspect

    from rebar.llm import runner as R

    src = inspect.getsource(R.PydanticAIRunner) + inspect.getsource(R._pai_structured)
    assert "_extract_structured" not in src
    assert 'output_strategy == "extract"' not in src
    # It dispatches through the deterministic layered stack (output_mode + parse_structured)
    # instead of a second interpreter LLM.
    assert "output_mode" in src and "parse_structured" in src
