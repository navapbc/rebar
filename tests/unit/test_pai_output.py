"""Contract tests for the Pydantic output-policy adapter (RP-01 S1, ticket
[rebar:grazeable-preferred-bumblebee]).

`pai_output` expresses two EXISTING deterministic authorities through documented
Pydantic AI seams, adding NO retry loop, provider selection, usage, or persistence:

  * `pai_output.output_function(model_cls)` -> a `TextOutput` output function that runs
    the deterministic `structured.parse_structured` and, on a retryable
    `StructuredOutputError`, raises Pydantic `ModelRetry` with the EXACT Rebar error in
    the exception cause chain (Pydantic AI drives the bounded output retry, not this
    module). A terminal `UnretryableOutputError` is re-raised as-is (never a retry).
  * `pai_output.guard_capability()` -> an `AbstractCapability` whose
    `after_model_request` hook runs `structured.check_response` on the full
    `ModelResponse`, so a refusal / truncation / content-filter turn raises the terminal
    Rebar subtype BEFORE the output text is processed and never becomes a retry prompt.

Every test drives a REAL Pydantic `Agent` over an offline `FunctionModel`, or the real
output function directly. No live/billable call can escape (ALLOW_MODEL_REQUESTS=False).
"""

from __future__ import annotations

import pytest

pytest.importorskip("pydantic_ai")

from pydantic import BaseModel, field_validator
from pydantic_ai import Agent, ModelRetry, TextOutput
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel

from rebar.llm import structured
from rebar.llm.errors import StructuredOutputError, UnretryableOutputError

pytestmark = pytest.mark.unit


class _Verdict(BaseModel):
    verdict: str
    confidence: float = 1.0

    @field_validator("confidence")
    @classmethod
    def _bound(cls, v: float) -> float:
        if not (0.0 <= v <= 1.0):
            raise ValueError("confidence must be in [0, 1]")
        return v


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    """No live/billable call can escape this module."""
    import pydantic_ai.models

    monkeypatch.setattr(pydantic_ai.models, "ALLOW_MODEL_REQUESTS", False)


def _agent(gen, *, retries: int = 0):
    """A real Agent wired with the adapter's output function + guard capability."""
    from rebar.llm import pai_output

    return Agent(
        FunctionModel(gen),
        output_type=TextOutput(pai_output.output_function(_Verdict)),
        capabilities=[pai_output.guard_capability()],
        retries={"output": retries} if retries else None,
    )


def _emit(*texts, finish_reason=None, provider_details=None):
    """Build a FunctionModel `gen` that emits `texts` in order and counts calls."""
    state = {"n": 0}

    def gen(messages, info):
        i = state["n"]
        state["n"] += 1
        text = texts[min(i, len(texts) - 1)]
        return ModelResponse(
            parts=[TextPart(text)],
            finish_reason=finish_reason,
            provider_details=provider_details,
        )

    gen.calls = state
    return gen


def _retry_prompt_present(result) -> bool:
    return any(
        "Retry" in type(p).__name__ for m in result.all_messages() for p in getattr(m, "parts", [])
    )


# ─────────────────────────── HAPPY PATH (given to implementer) ───────────────────────────


def test_agent_returns_typed_object_for_valid_native_json():
    """A real Agent using the adapter returns the expected typed object for a clean
    (native-style) JSON reply. AC#1."""
    gen = _emit('{"verdict": "PASS", "confidence": 0.9}')
    result = _agent(gen).run_sync("evaluate")
    assert result.output == _Verdict(verdict="PASS", confidence=0.9)
    assert gen.calls["n"] == 1


def test_agent_returns_typed_object_for_prompted_freetext():
    """A prose-wrapped / fenced (prompted-style) reply still yields the typed object
    through the deterministic tolerant-parse path. AC#1."""
    gen = _emit('Sure! Here it is:\n```json\n{"verdict": "FAIL"}\n```\nHope that helps.')
    result = _agent(gen).run_sync("evaluate")
    assert result.output == _Verdict(verdict="FAIL", confidence=1.0)


# ─────────────────────────── HELD-OUT ORACLE (edge + e2e) ────────────────────────────────

# AC#2: the historical repair / sentinel / decoy corpus produces the SAME values through
# the adapter's output function as through the pure `structured.parse_structured`.
_CORPUS = {
    "strict": '{"verdict": "PASS"}',
    "markdown_fence": '```json\n{"verdict": "PASS"}\n```',
    "trailing_comma": '{"verdict": "PASS",}',
    "unclosed_brace": '{"verdict": "PASS"',
    "single_quotes": "{'verdict': 'PASS'}",
    "prose_wrapped": 'Sure! Here is the result: {"verdict": "PASS"} — hope that helps.',
    "sentinel": ('prose before\n<<<REBAR_OUTPUT>>>\n{"verdict": "PASS"}\n<<<END>>>\nprose after'),
    "decoy_then_valid": ('{"relation": "depends_on"} then the real answer {"verdict": "PASS"}'),
}


@pytest.mark.parametrize("name", sorted(_CORPUS))
def test_output_function_matches_pure_parser_on_corpus(name):
    """AC#2: adapter output-function value == structured.parse_structured value, so the
    deterministic repair/sentinel/decoy selection is preserved byte-for-byte."""
    from rebar.llm import pai_output

    text = _CORPUS[name]
    expected = structured.parse_structured(text, _Verdict)
    got = pai_output.output_function(_Verdict)(text)
    assert got == expected


def test_retryable_validation_failure_preserves_exact_error_in_cause(monkeypatch):
    """AC#3: a retryable StructuredOutputError from the parser is preserved as the EXACT
    object in the raised ModelRetry's cause chain (identity, not just type)."""
    from rebar.llm import pai_output

    sentinel = StructuredOutputError("bespoke validation failure XYZ")

    def _boom(text, model_cls):
        raise sentinel

    monkeypatch.setattr(structured, "parse_structured", _boom)
    with pytest.raises(ModelRetry) as excinfo:
        pai_output.output_function(_Verdict)("anything")
    assert excinfo.value.__cause__ is sentinel


def test_output_function_does_not_wrap_unretryable_in_modelretry(monkeypatch):
    """AC#4 (unit): a terminal UnretryableOutputError from the parse path is re-raised
    as-is and NEVER converted into a Pydantic ModelRetry."""
    from rebar.llm import pai_output

    terminal = UnretryableOutputError("refused/truncated")

    def _boom(text, model_cls):
        raise terminal

    monkeypatch.setattr(structured, "parse_structured", _boom)
    with pytest.raises(UnretryableOutputError) as excinfo:
        pai_output.output_function(_Verdict)("anything")
    assert excinfo.value is terminal


def test_invalid_then_valid_drives_pydantic_bounded_retry(monkeypatch):
    """AC#1/#3 (e2e): invalid-then-valid output drives Pydantic's own bounded output
    retry (via ModelRetry) and returns the typed object; a retry prompt was emitted."""
    gen = _emit("not json at all", '{"verdict": "PASS"}')
    result = _agent(gen, retries=2).run_sync("evaluate")
    assert result.output == _Verdict(verdict="PASS")
    assert gen.calls["n"] == 2
    assert _retry_prompt_present(result)


def test_refusal_finish_reason_is_terminal_without_retry_prompt():
    """AC#4: a content_filter/refusal turn is caught by the after_model_request guard,
    raises the terminal Rebar subtype after exactly ONE model call, and emits NO retry
    prompt (the guard fires before the output text is processed)."""
    from rebar.llm import pai_output

    gen = _emit('{"verdict": "PASS"}', finish_reason="content_filter")
    agent = Agent(
        FunctionModel(gen),
        output_type=TextOutput(pai_output.output_function(_Verdict)),
        capabilities=[pai_output.guard_capability()],
        retries={"output": 2},
    )
    with pytest.raises(UnretryableOutputError):
        agent.run_sync("evaluate")
    assert gen.calls["n"] == 1


def test_truncation_length_is_terminal_one_call():
    """AC#4: a `length` (max_tokens) truncation is terminal — one call, no retry."""
    from rebar.llm import pai_output

    gen = _emit('{"verdict": "PA', finish_reason="length")
    agent = Agent(
        FunctionModel(gen),
        output_type=TextOutput(pai_output.output_function(_Verdict)),
        capabilities=[pai_output.guard_capability()],
        retries={"output": 2},
    )
    with pytest.raises(UnretryableOutputError):
        agent.run_sync("evaluate")
    assert gen.calls["n"] == 1


def test_provider_details_refusal_defense_in_depth():
    """AC#4 (defense-in-depth): a refusal signalled only in provider_details (finish_reason
    left normal — the pydantic-ai #5221 shape) is still caught terminally by the guard."""
    from rebar.llm import pai_output

    gen = _emit(
        '{"verdict": "PASS"}',
        finish_reason="stop",
        provider_details={"finish_reason": "refusal", "refusal": "policy"},
    )
    agent = Agent(
        FunctionModel(gen),
        output_type=TextOutput(pai_output.output_function(_Verdict)),
        capabilities=[pai_output.guard_capability()],
        retries={"output": 2},
    )
    with pytest.raises(UnretryableOutputError):
        agent.run_sync("evaluate")
    assert gen.calls["n"] == 1


def test_transient_error_finish_reason_retries_not_aborts():
    """AC#4 (parity with the bespoke loop): finish_reason='error' is a TRANSIENT provider
    error that structured.check_response maps to a RETRYABLE StructuredOutputError, NOT the
    terminal UnretryableOutputError. The guard must translate it into Pydantic's bounded
    retry, so an error-then-clean turn sequence retries the model and returns the typed
    object rather than aborting the run on a non-ModelRetry exception."""
    from rebar.llm import pai_output

    state = {"n": 0}

    def gen(messages, info):
        state["n"] += 1
        finish_reason = "error" if state["n"] == 1 else "stop"
        return ModelResponse(parts=[TextPart('{"verdict": "PASS"}')], finish_reason=finish_reason)

    agent = Agent(
        FunctionModel(gen),
        output_type=TextOutput(pai_output.output_function(_Verdict)),
        capabilities=[pai_output.guard_capability()],
        retries={"output": 2},
    )
    result = agent.run_sync("evaluate")
    assert result.output == _Verdict(verdict="PASS")
    assert state["n"] == 2


def test_pai_output_adds_no_scheduling_usage_or_persistence():
    """AC#5 (architectural contract): pai_output must not couple to the retry scheduler,
    usage accounting, or persistence layers — expressed as a module-dependency guard so it
    stays true under refactors (no retry loop / provider selection / usage / persistence)."""
    import ast
    from pathlib import Path

    from rebar.llm import pai_output

    src = Path(pai_output.__file__).read_text()
    imported: set[str] = set()
    for node in ast.walk(ast.parse(src)):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
    forbidden = {
        "rebar.llm.usage_log",
        "rebar.llm.structured_run",
        "rebar.llm.runner",
        "rebar.llm.agent_call",
    }
    assert not (imported & forbidden), f"pai_output must not import {imported & forbidden}"
