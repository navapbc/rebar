"""Provider-native schema compilation can reject complex verification contracts.
These tests pin the public output-mode fallback and translate both Bedrock grammar
timeouts and schema-complexity errors without reporting a provider outage. The
bound stays below the published optional-parameter limit because a 22-optional
verification contract failed in the measured provider call.
"""

from __future__ import annotations

import pytest

pytest.importorskip("pydantic_ai", reason="structured-output modes need the [agents] extra")

from pydantic import BaseModel
from pydantic_ai import NativeOutput, PromptedOutput

from rebar.llm import capabilities, contracts, structured
from rebar.llm.errors import LLMUnavailableError

# The model whose capability row makes native structured output reachable at all (story 18ae).
# Named as a literal so this test fails loudly if the row is retargeted rather than passing
# vacuously against some other model that was never native.
_NATIVE_MODEL = "bedrock:us.anthropic.claude-sonnet-4-6"

# The two contracts MEASURED to fail native compilation on that model, by registry name.
_OVER_COMPLEX_CONTRACTS = ("plan_review_verification", "code_review_verification")


def _native_caps():
    """The REAL capability record for the native-capable cell, read through the production
    path (not a hand-built stub), so this test breaks if that path stops reporting native."""
    caps = capabilities.capabilities_for(_NATIVE_MODEL)
    assert caps.native_structured_output is True, (
        f"{_NATIVE_MODEL} is expected to report native structured output (story 18ae); "
        "without that this test would pass vacuously — every model would route PromptedOutput"
    )
    return caps


class _SmallContract(BaseModel):
    """A deliberately tiny contract: 1 optional parameter. MEASURED to compile in 2.0s."""

    index: int
    ok: bool = True


def _optional_count(model_cls) -> int:
    schema = model_cls.model_json_schema()
    total = 0

    def walk(node):
        nonlocal total
        if isinstance(node, dict):
            if node.get("type") == "object" or "properties" in node:
                props = node.get("properties", {}) or {}
                required = set(node.get("required", []) or [])
                total += len([p for p in props if p not in required])
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for value in node:
                walk(value)

    walk(schema)
    return total


@pytest.mark.parametrize("contract_name", _OVER_COMPLEX_CONTRACTS)
def test_output_mode_refuses_native_for_a_contract_the_provider_cannot_compile(contract_name):
    """THE REGRESSION. Both gates' Pass-2 verification contracts exceed the provider's grammar
    budget, so ``output_mode`` must route them to PromptedOutput EVEN THOUGH the model's
    capability record says native is available.

    Asserted per-contract (not over an aggregate) so a fix that rescues plan-review while
    leaving the code-review gate broken still fails here — those are two different gates and
    the code-review one casts the Gerrit ``LLM-Review`` vote.
    """
    import rebar.llm.plan_review.passes

    try:
        import rebar.llm.code_review.registry  # noqa: F401
    except ImportError:  # pragma: no cover - optional registration path
        pass

    model_cls = contracts.response_model_for(contract_name)
    # Guard the fixture itself: if the contract ever slims below the bound this test would
    # start passing for the WRONG reason (nothing to refuse), so pin why it must be refused.
    assert _optional_count(model_cls) > 21, (
        f"{contract_name} is expected to exceed the measured native bound; if it was "
        "deliberately slimmed, re-measure and update this test rather than deleting it"
    )

    mode = structured.output_mode(model_cls, _native_caps())

    assert isinstance(mode, PromptedOutput), (
        f"{contract_name} has {_optional_count(model_cls)} optional parameters and is MEASURED "
        f"to fail native grammar compilation on {_NATIVE_MODEL} (HTTP 400, ~185s). "
        f"output_mode returned {type(mode).__name__}; it must return PromptedOutput."
    )


def test_output_mode_still_routes_native_for_a_small_contract():
    """The opposite pole, and the reason this gate is not just 'disable native'.

    Story 18ae enabled native output on this cell for a measured reason and small contracts
    genuinely compile (2.0s measured). A fix that blanket-disables native would pass the test
    above while destroying 18ae's benefit — this case fails it.
    """
    mode = structured.output_mode(_SmallContract, _native_caps())

    assert isinstance(mode, NativeOutput), (
        "a small contract must still route NativeOutput on a native-capable model; the "
        f"complexity gate must be a BOUND, not a blanket disable (got {type(mode).__name__})"
    )


def test_a_non_native_model_is_unaffected_by_the_complexity_gate():
    """Contrast: the gate must not change routing for a model that was never native. Without
    this, a fix that returns PromptedOutput unconditionally would look correct."""
    import rebar.llm.plan_review.passes  # noqa: F401

    caps = capabilities.capabilities_for("bedrock:us.anthropic.claude-opus-4-8")
    assert caps.native_structured_output is False, "opus-4-8 is expected to stay fail-closed"

    mode = structured.output_mode(contracts.response_model_for("plan_review_verification"), caps)
    assert isinstance(mode, PromptedOutput)


def test_schema_complexity_rejection_is_translated_not_reported_as_an_outage():
    """Schema-complexity rejection is permanent for the request, so it must not be
    reported as a transient outage. The translator handles both pydantic-ai's
    status_code attribute and boto3's text-only ValidationException.
    """
    from rebar.llm.failure import translate_schema_complexity_rejection

    class _ModelHTTPErrorLike(Exception):
        status_code = 400

    grammar_timeout = _ModelHTTPErrorLike(
        "status_code: 400, model_name: us.anthropic.claude-sonnet-4-6, body: {'Error': "
        "{'Message': 'The model returned the following errors: Grammar compilation timed "
        "out.', 'Code': 'ValidationException'}}"
    )
    too_complex = Exception(
        "An error occurred (ValidationException) when calling the Converse operation: The "
        "model returned the following errors: Schema is too complex."
    )

    for exc in (grammar_timeout, too_complex):
        err = translate_schema_complexity_rejection(exc, "us.anthropic.claude-sonnet-4-6")
        assert err is not None, f"not translated: {exc}"
        assert not isinstance(err, LLMUnavailableError), (
            "a schema-complexity rejection must NOT be classified as a provider outage — "
            "the provider is up and the same request will fail identically forever"
        )
        assert "us.anthropic.claude-sonnet-4-6" in str(err), (
            "must name the model the operator has to act on"
        )


def test_unrelated_provider_failures_are_not_swallowed_by_the_translation():
    """Contrast case. Without it the translation could be a catch-all that hides real outages —
    the precise failure mode this ticket is fixing, inverted."""
    from rebar.llm.failure import translate_schema_complexity_rejection

    class _Rejection400(Exception):
        status_code = 400

    for unrelated in (
        _Rejection400("ValidationException: 'temperature' is deprecated for this model"),
        _Rejection400("ValidationException: malformed request"),
        Exception("status_code: 500, body: {'Error': {'Message': 'internal failure'}}"),
        Exception("Connection reset by peer"),
        LLMUnavailableError("the LLM provider call failed: throttled"),
    ):
        assert (
            translate_schema_complexity_rejection(unrelated, "us.anthropic.claude-sonnet-4-6")
            is None
        ), f"must not translate an unrelated failure: {unrelated}"


# Botocore can reject native outputConfig before sending a request, even for a
# small contract. This client-side message differs from provider grammar failures.
# The mutation-checked tests cover that separate translation rule.


def test_the_client_side_outputConfig_rejection_is_also_translated():
    """Variant B. Same defect (native output enabled where it cannot work), different wall."""
    from rebar.llm.failure import translate_schema_complexity_rejection

    via_botocore = Exception(
        "Parameter validation failed:\n"
        'Unknown parameter in input: "outputConfig", must be one of: modelId, messages, '
        "system, inferenceConfig, toolConfig, guardrailConfig, additionalModelRequestFields"
    )
    err = translate_schema_complexity_rejection(via_botocore, "us.anthropic.claude-sonnet-4-6")
    assert err is not None, (
        "the 0.0s client-side outputConfig rejection must be recognised; it is the SAME root "
        "cause as the 185s server-side one and shares none of its wording"
    )
    assert not isinstance(err, LLMUnavailableError), (
        "an under-versioned botocore is not a provider outage — the provider was never reached"
    )


def test_an_unrelated_botocore_parameter_failure_is_not_swallowed():
    """Contrast for variant B: the rule must key on outputConfig, not on any param failure."""
    from rebar.llm.failure import translate_schema_complexity_rejection

    unrelated = Exception(
        "Parameter validation failed:\n"
        'Unknown parameter in input: "inferenceConfig", must be one of: modelId, messages'
    )
    assert (
        translate_schema_complexity_rejection(unrelated, "us.anthropic.claude-sonnet-4-6") is None
    )
