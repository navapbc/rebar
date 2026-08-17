"""RP-01 S3 — HAPPY-PATH oracle for typed exhaustion restoration
(ticket [rebar:serge-monotonous-aruanas], 558e-d9bd-e285-40a1).

This is the ONLY S3 behavioral test the implementer sees; the edge/negative/robustness cases
live in ``test_rp01_s3_contract_oracle.py`` (held out).

The one behavior it pins is the headline of AC2: when the bounded Agent operation exhausts its
output-retry budget on a VALIDATION failure, the terminating ``UnexpectedModelBehavior`` carries
the original Rebar :class:`StructuredOutputError` on its standard Python cause chain (because
S2's ``pai_output.output_function`` raises ``ModelRetry(str(err)) from err``). S3 walks that
chain and re-raises the IDENTICAL object — same ``id()`` — preserving its subtype and any
attributes, instead of constructing a fresh, information-losing error.

Driven through the REAL ``PydanticAIRunner`` over an offline ``FunctionModel``; no live/billable
call can escape (ALLOW_MODEL_REQUESTS off). The exact-object identity is pinned by planting a
recognizable sentinel ``StructuredOutputError`` at the parser seam and asserting the runner
re-raises THAT object.
"""

from __future__ import annotations

import pytest

pytest.importorskip("pydantic_ai")

from rebar.llm.config import LLMConfig
from rebar.llm.errors import StructuredOutputError
from rebar.llm.runner import PydanticAIRunner, RunRequest

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _offline(monkeypatch):
    import pydantic_ai.models

    monkeypatch.setattr(pydantic_ai.models, "ALLOW_MODEL_REQUESTS", False)


def _scripted_model(text="anything"):
    """A ``FunctionModel`` returning the same benign, well-formed-looking text every call. The
    parser is what fails (planted below), so the model itself never signals refusal/truncation
    — the run fails purely on repeated validation until the output-retry budget is exhausted."""
    from pydantic_ai.messages import ModelResponse, TextPart
    from pydantic_ai.models.function import FunctionModel

    def gen(messages, info):
        return ModelResponse(parts=[TextPart(text)])

    return FunctionModel(gen)


def _req(cfg):
    return RunRequest(
        system_prompt="x",
        instructions="y",
        config=cfg,
        reviewers=["v"],
        mode="structured",
        output_schema="completion_verdict",
    )


def _run(model, *, cfg=None):
    cfg = cfg or LLMConfig(repo_path=".")
    return PydanticAIRunner(cfg, model_override=model).run(_req(cfg))


def test_validation_exhaustion_reraises_the_identical_rebar_error(monkeypatch):
    """AC2 (RED-first): a repeated validation failure exhausts the output-retry budget; the
    ORIGINAL Rebar ``StructuredOutputError`` is on the terminating exception's cause chain and
    is re-raised by identity (``is``), not reconstructed. Today the translator builds a fresh
    ``UnretryableOutputError``, so ``ei.value is sentinel`` is False — the right RED reason."""
    from rebar.llm import structured

    sentinel = StructuredOutputError("SENTINEL: output could not be parsed")
    sentinel.s3_identity_probe = "unique-marker"  # type: ignore[attr-defined]

    def _always_fail(text, model_cls):
        raise sentinel

    # `output_function` calls `structured.parse_structured(text, model_cls)`; a plain (retryable)
    # StructuredOutputError there is translated to `ModelRetry(str(err)) from err`, so the
    # sentinel rides the chain to exhaustion. (String-named seam — patched by attribute.)
    monkeypatch.setattr(structured, "parse_structured", _always_fail)

    with pytest.raises(StructuredOutputError) as ei:
        _run(_scripted_model())

    assert ei.value is sentinel, (
        "validation exhaustion must re-raise the IDENTICAL original StructuredOutputError "
        "found on the terminating UnexpectedModelBehavior's cause chain, not a fresh object"
    )
    assert getattr(ei.value, "s3_identity_probe", None) == "unique-marker"
