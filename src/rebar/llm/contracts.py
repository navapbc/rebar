"""Per-operation structured-output **contracts** — the seam that lets each operation
(and each workflow agent step) declare its OWN structured-output shape instead of the
runner hardcoding the findings model.

A contract maps a **serializable name** (the same string used as ``RunRequest.output_schema``
and as the JSON Schema name) to a builder that returns the Pydantic model the runner binds
as its structured-output contract. We key by NAME rather than passing the model object
directly (as Pydantic AI / instructor / OpenAI Agents SDK do) because ``output_schema`` is
threaded from the workflow YAML DSL, where a live class can't live — so the name is the
portable handle, and a schema-pin test keeps each model in lock-step with its JSON Schema.

Import-clean: the only module-top import is :mod:`rebar.llm.findings` (stdlib-only); every
builder imports ``pydantic`` **inside its body**, and registration merely stores a callable —
so ``import rebar.llm`` / ``import rebar.llm.contracts`` pull no heavy dependency.
"""

from __future__ import annotations

from collections.abc import Callable

from rebar.llm import findings

# name -> zero-arg builder returning a pydantic BaseModel subclass (the response model).
_CONTRACTS: dict[str, Callable[[], type]] = {}


def register_contract(name: str, builder: Callable[[], type]) -> None:
    """Register ``builder`` (a zero-arg factory returning a pydantic model) under ``name``.
    Storing a callable only — no model is built and no pydantic import happens here."""
    _CONTRACTS[name] = builder


def response_model_for(output_schema: str | None) -> type:
    """The structured-output Pydantic model for ``output_schema`` (a registered contract
    name), or the **findings** model default when it is unset/unknown. Built lazily — the
    selected builder imports pydantic internally."""
    if output_schema and output_schema in _CONTRACTS:
        return _CONTRACTS[output_schema]()
    return findings.findings_response_model()


def completion_verdict_response_model() -> type:
    """Structured-output model for the completion-verification op — mirrors
    ``completion_verdict.schema.json`` (pinned by a test). Reuses the shared ``Citation``
    model (no drift) and adds a per-finding ``criterion`` (the specific requirement that
    failed). pydantic imported lazily."""
    from pydantic import BaseModel, Field

    Citation = findings.citation_model()

    class VerdictFinding(BaseModel):
        criterion: str = Field(
            description="The specific criterion that failed (verbatim or clearly identifying)."
        )
        detail: str = Field(description="Explanation of why the criterion is not met.")
        severity: str = Field(default="high", description="critical | high | medium | low | info.")
        dimension: str = Field(default="completion", description="Finding dimension.")
        citations: list[Citation] = Field(
            default_factory=list, description="Evidence: file+line / url / freeform source."
        )
        title: str | None = Field(default=None, description="Optional short headline.")

    class CompletionVerdict(BaseModel):
        """Structured output of the completion verifier: a PASS/FAIL verdict and, on FAIL,
        one finding per failing criterion."""

        verdict: str = Field(description="PASS or FAIL (normalized by the operation).")
        findings: list[VerdictFinding] = Field(
            default_factory=list, description="One per FAILING criterion; empty on PASS."
        )
        summary: str | None = Field(
            default=None, description="Optional summary / no-explicit-criteria PASS rationale."
        )

    return CompletionVerdict


# Built-ins. ``review_result`` (the default findings shape) and ``completion_verdict``.
register_contract("review_result", findings.findings_response_model)
register_contract("completion_verdict", completion_verdict_response_model)
