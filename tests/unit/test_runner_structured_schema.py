"""The prompted structured-output path MUST convey the output schema to the model.

Root cause (epic 5ca8 / joe-debug): the Anthropic ``PromptedOutput`` path in
``_pai_structured`` generated FREE TEXT without telling the model the JSON shape, so
the model emitted plausible-but-divergent keys (e.g. ``{"findings": [{"attributes",
"sub_answers"}]}`` instead of ``{"verifications": [{"severity_attributes",
"binary"}]}``). Tolerant parsing then found no ``verifications`` key and defaulted to
an EMPTY list — so every plan-review finding got ``no-verification`` and the verdict
was forced INDETERMINATE.

This test pins the load-bearing CONTRACT: a ``mode="structured"`` run conveys the
output schema to the model. The FunctionModel emits the correct shape ONLY when the
schema reached its prompt, and otherwise reproduces the real divergent shape — so the
run yields usable structured output iff the schema was conveyed.
"""

from __future__ import annotations

import pytest

from rebar.llm.config import LLMConfig
from rebar.llm.runner import PydanticAIRunner, RunRequest

pytest.importorskip("pydantic_ai")

# The real divergent shape the live model emitted when it was NOT told the schema:
# right inner fields, WRONG wrapper/group keys → parses to an empty VerificationOutput.
_WRONG_SHAPE = '{"findings": [{"index": 0, "attributes": {}, "sub_answers": {}}]}'
# A schema-conforming verification (one finding at index 0).
_RIGHT_SHAPE = (
    '{"verifications": [{"index": 0, '
    '"severity_attributes": {"prod_impact": "low", "debt_impact": "low", '
    '"blast_radius": "local", "likelihood": "low", "reversibility": "easy"}, '
    '"binary": {"is_verifiable": "yes", "evidence_entails_finding": "yes", '
    '"path_reachable": "yes", "impact_follows_necessarily": "yes", '
    '"no_viable_alternative_explanation": "yes", "no_existing_mitigation": "yes", '
    '"severity_claim_justified": "yes", "cited_reference_accurate": "na"}}]}'
)


def _schema_aware_model():
    """A FunctionModel that returns a schema-CONFORMING verification iff the output
    schema (its distinctive snake_case key ``severity_attributes``) reached the prompt
    the model received; otherwise it reproduces the real divergent shape."""
    from pydantic_ai.messages import ModelResponse, TextPart
    from pydantic_ai.models.function import FunctionModel

    def gen(messages, info):
        text = " ".join(
            str(getattr(p, "content", "")) for m in messages for p in getattr(m, "parts", [])
        )
        schema_conveyed = "severity_attributes" in text and "verifications" in text
        return ModelResponse(parts=[TextPart(_RIGHT_SHAPE if schema_conveyed else _WRONG_SHAPE)])

    return FunctionModel(gen)


def _verify_req() -> RunRequest:
    # Mirrors the plan-review Pass-2 verify step: structured mode, the verification
    # contract, and a prompt that (like the real verifier prompt) describes fields only
    # in PROSE — it does NOT spell out the JSON keys.
    return RunRequest(
        system_prompt="You are a verifier. Emit severity ATTRIBUTES and BINARY sub-answers.",
        instructions="### finding index 0\nclaim: X\ncriteria: E5\nevidence: \nimpact: ",
        config=LLMConfig(repo_path="."),
        reviewers=["plan-review-verifier-agentic"],
        mode="structured",
        output_schema="verification",
    )


def test_prompted_structured_conveys_schema_so_verifications_are_extracted() -> None:
    out = PydanticAIRunner(LLMConfig(repo_path="."), model_override=_schema_aware_model()).run(
        _verify_req()
    )
    # The verifier must produce a usable verification keyed by index — NOT an empty list
    # (which is what an un-conveyed schema yields, the no-verification bug).
    assert out.get("verifications"), (
        "structured run produced no verifications — the output schema was not conveyed "
        f"to the model (got: {out.get('verifications')!r})"
    )
    assert out["verifications"][0]["index"] == 0
