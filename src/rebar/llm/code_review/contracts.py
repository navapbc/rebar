"""Structured-output contract for the code-review BASE reviewer (epic b744 / WS1).

The live ``PydanticAIRunner`` binds an agent step's structured-output shape via
``contracts.response_model_for(output_schema)`` — which keys off the CONTRACTS registry,
NOT the JSON-Schema registry. So a prompt that declares ``outputs: code_review_base_output``
only emits the right fields if a Pydantic model is REGISTERED under that name here; without
it the runner falls back to the default findings+summary model and ``recommend_overlays`` is
structurally impossible to emit. This module registers that model (mirroring
``plan_review/passes.py``'s ``register_contracts()``), so BOTH named outputs — kernel-shaped
``findings`` and the bounded ``recommend_overlays`` escalation — survive into the workflow.

The JSON Schema (``code_review_base_output.schema.json``) stays the permissive post-hoc
validator; THIS model is the generation-time contract. ``overlay_id`` is a plain ``str`` (not
a ``Literal`` over the catalog) on purpose — the closed ``OVERLAY_IDS`` enum is enforced
post-hoc by ``registry.filter_recommend_overlays`` (drop-not-error), so a hallucinated id
costs nothing rather than failing the whole step.
"""

from __future__ import annotations

from rebar.llm import contracts


def base_output_model() -> type:
    """The base-reviewer response model: kernel Pass-1 findings (claim/criteria/evidence/
    impact — the shape the kernel Pass-2 listing consumes) PLUS ``recommend_overlays`` and a
    ``summary``. pydantic imported lazily so importing this module stays cheap."""
    from pydantic import BaseModel, Field

    class CodeFinding(BaseModel):
        finding: str = Field(description="The defect/gap, stated as a claim to verify.")
        criteria: list[str] = Field(
            default_factory=list, description="Code-review dimension id(s) the finding maps to."
        )
        location: str = Field(
            default="",
            description="WHERE: the changed-file path / `path:line` the finding is about.",
        )
        evidence: list[str] = Field(
            default_factory=list,
            description="A LIST of grounding strings: a code quote, a `path:line` citation, or an "
            "ABSENCE rationale. Always a list (never a bare string) — the kernel joins it.",
        )
        scenarios: list[str] = Field(default_factory=list, description="Where this bites.")
        impact: str = Field(default="", description="Consequence if unaddressed.")
        checklist_item: str = Field(
            default="", description="The finding expressed as ONE actionable `- [ ]` line."
        )
        suggested_fix: str = Field(
            default="", description="A concrete fix — ONLY when confident; else empty."
        )

    class OverlayRecommendation(BaseModel):
        overlay_id: str = Field(
            description="A specialist overlay id to ALSO run (validated post-hoc against the "
            "closed OVERLAY_IDS catalog; unknown ids are dropped, not errored)."
        )
        reason: str = Field(description="One-line justification for escalating to this overlay.")

    class CodeReviewBaseOutput(BaseModel):
        analysis: str = Field(default="", description="Scratchpad — reason before emitting.")
        findings: list[CodeFinding] = Field(default_factory=list)
        recommend_overlays: list[OverlayRecommendation] = Field(
            default_factory=list,
            description="Bounded base->overlay escalation signal (may be empty).",
        )
        summary: str | None = Field(default=None, description="Optional short summary.")

    return CodeReviewBaseOutput


def register_contracts() -> None:
    """Register the code-review structured-output contract(s). Idempotent."""
    contracts.register_contract("code_review_base_output", base_output_model)


register_contracts()
