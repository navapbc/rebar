"""Structured-output contracts for the code-review gate (epic b744 / WS1 + WS2).

The live ``PydanticAIRunner`` binds an agent step's structured-output shape via
``contracts.response_model_for(output_schema)`` — keyed off the CONTRACTS registry, NOT the
JSON-Schema registry. So a prompt that declares ``outputs: <name>`` only emits the right
fields if a Pydantic model is REGISTERED here. This module registers the code-review
contracts (mirroring ``plan_review/passes.py``'s ``register_contracts()``):

- ``code_review_base_output`` (WS1 base reviewer) — kernel-shaped ``findings`` + the bounded
  ``recommend_overlays`` escalation.
- ``code_review_findings`` (WS2 overlay finders) — kernel-shaped ``findings`` only (overlays
  do not re-escalate; one-hop).
- ``code_review_coach`` (WS2 Pass-4 coach) — move-picks ``[{move_id, subject, finding_refs}]``.

The Pass-2 verifier reuses the kernel's gate-agnostic ``verification`` contract (registered by
``review_kernel.verify``), so it is NOT re-registered here.

Findings use the kernel Pass-1 shape (claim/criteria/evidence/impact — what the kernel Pass-2
listing consumes); ``evidence`` is a ``list[str]`` so the kernel's ``' | '.join(...)`` cannot
crash. ``overlay_id`` stays a plain ``str`` (the closed ``OVERLAY_IDS`` enum is enforced
post-hoc by ``registry.filter_recommend_overlays`` — drop-not-error)."""

from __future__ import annotations

from rebar.llm import contracts


def _code_finding_model() -> type:
    """The kernel Pass-1 finding shape for code review (built lazily; pydantic imported here)."""
    from pydantic import BaseModel, Field

    class CodeFinding(BaseModel):
        finding: str = Field(description="The defect/gap, stated as a claim to verify.")
        criteria: list[str] = Field(
            default_factory=list,
            description="Code-review dimension/overlay id(s) the finding maps to.",
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

    return CodeFinding


def base_output_model() -> type:
    """Base reviewer (WS1): kernel findings PLUS ``recommend_overlays`` + ``summary``."""
    from pydantic import BaseModel, Field

    CodeFinding = _code_finding_model()

    class OverlayRecommendation(BaseModel):
        overlay_id: str = Field(
            description="A specialist overlay id to ALSO run (validated post-hoc against the "
            "closed OVERLAY_IDS catalog; unknown ids are dropped, not errored)."
        )
        reason: str = Field(description="One-line justification for escalating to this overlay.")

    class CodeReviewBaseOutput(BaseModel):
        analysis: str = Field(default="", description="Scratchpad — reason before emitting.")
        findings: list[CodeFinding] = Field(default_factory=list)  # type: ignore[valid-type]
        recommend_overlays: list[OverlayRecommendation] = Field(
            default_factory=list,
            description="Bounded base->overlay escalation signal (may be empty).",
        )
        summary: str | None = Field(default=None, description="Optional short summary.")

    return CodeReviewBaseOutput


def findings_model() -> type:
    """Overlay finders (WS2): kernel findings only — overlays do not re-escalate (one-hop)."""
    from pydantic import BaseModel, Field

    CodeFinding = _code_finding_model()

    class CodeReviewFindings(BaseModel):
        analysis: str = Field(default="", description="Scratchpad — reason before emitting.")
        findings: list[CodeFinding] = Field(default_factory=list)  # type: ignore[valid-type]
        summary: str | None = Field(default=None, description="Optional short summary.")

    return CodeReviewFindings


def coach_model() -> type:
    """Pass-4 coach (WS2): move-picks; the kernel renders the prose deterministically."""
    from pydantic import BaseModel, Field

    class CodeCoachNote(BaseModel):
        move_id: str = Field(description="A move id from the locked code move-catalog.")
        subject: str = Field(
            description="A short noun-phrase subject (≤8 words; no code, no imperative)."
        )
        finding_refs: list[str] = Field(
            default_factory=list, description="The finding id(s) this move addresses."
        )

    class CodeCoachOutput(BaseModel):
        notes: list[CodeCoachNote] = Field(default_factory=list)

    return CodeCoachOutput


def register_contracts() -> None:
    """Register the code-review structured-output contracts. Idempotent."""
    contracts.register_contract("code_review_base_output", base_output_model)
    contracts.register_contract("code_review_findings", findings_model)
    contracts.register_contract("code_review_coach", coach_model)


register_contracts()
