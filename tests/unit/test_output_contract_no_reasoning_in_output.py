"""No output contract may ask the model to write its reasoning into the response.

Measured 2026-11-09 (epic pompous-vitriolic-macaw, ticket beaten-natural-grouse): Bedrock Opus 5.5
declines (`stopReason: content_filtered`) a request whose output schema describes a field as
"Scratchpad — reason before emitting." or "REASON FIRST: brief reasoning ... before the
answers", consistent with Anthropic's `reasoning_extraction` safeguard. Replacing only that
description with neutral wording made the same captured request complete normally, and the plan
review and code review evals that were declined then passed. 5.5 models reason in thinking
blocks, so a field that asks for the reasoning in the output text is both unnecessary and a
refusal trigger on the production provider.

The scan covers every registered contract, so a new contract cannot reintroduce the pattern.
"""

from __future__ import annotations

import json
import re

import pytest

pytestmark = pytest.mark.unit

# Phrasings that ask the model to externalize its reasoning in the response body.
_REASONING_IN_OUTPUT = re.compile(
    r"scratchpad|reason (first|before|step)|reasoning (through|before)|think (step|before)"
    r"|chain[- ]of[- ]thought|show (your|the) (reasoning|work)",
    re.IGNORECASE,
)


def _registered_contracts() -> dict:
    import importlib

    from rebar.llm import contracts

    # Importing these modules runs their `register_contract` calls.
    for module in (
        "rebar.llm.code_review.contracts",
        "rebar.llm.plan_review.agentic",
        "rebar.llm.plan_review.floors",
        "rebar.llm.plan_review.passes",
        "rebar.llm.plan_review.xcheck",
        "rebar.llm.review_kernel.verify_models",
    ):
        importlib.import_module(module)

    return dict(contracts._CONTRACTS)


def _descriptions(schema: object):
    if isinstance(schema, dict):
        desc = schema.get("description")
        if isinstance(desc, str):
            yield desc
        for value in schema.values():
            yield from _descriptions(value)
    elif isinstance(schema, list):
        for value in schema:
            yield from _descriptions(value)


def test_the_scan_sees_the_contracts_that_carried_the_pattern() -> None:
    """Guard the guard: the contracts that had the scratchpad field must be in the scan set."""
    names = set(_registered_contracts())
    assert "plan_review_findings" in names, sorted(names)
    assert len(names) >= 10, sorted(names)


@pytest.mark.parametrize("name", sorted(_registered_contracts()))
def test_no_output_field_asks_for_reasoning_in_the_response(name) -> None:
    model = _registered_contracts()[name]()
    schema = model.model_json_schema()
    offending = [d for d in _descriptions(schema) if _REASONING_IN_OUTPUT.search(d)]
    assert not offending, f"{name}: {json.dumps(offending)}"


@pytest.mark.parametrize(
    "text",
    [
        "Scratchpad — reason before emitting.",
        "REASON FIRST: brief reasoning through this finding's sub-questions before the answers.",
    ],
)
def test_the_pattern_matches_the_measured_triggers(text) -> None:
    assert _REASONING_IN_OUTPUT.search(text)


def test_the_pattern_spares_the_measured_safe_wording() -> None:
    for text in ("Brief notes on the change.", "A short reason (≤1 sentence)."):
        assert not _REASONING_IN_OUTPUT.search(text), text
