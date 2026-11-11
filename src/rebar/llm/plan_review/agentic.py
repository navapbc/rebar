"""The agentic plan-review engine (ADR 0123): one read-only agent reviews the plan against the
code it will touch and returns a finding list.

The selectable switch (``[verify] plan_review_engine``) routes a DET-passing review here instead
of the four-pass pipeline. The gate workflow's agentic arm (``gates/plan-review.yaml``) is:

    plan_review_agentic_inputs (uses)     # render the plan + the criteria context
    agentic_review <prompt: plan-review-agentic>   # ONE agent call, read-only tools
    plan_review_agentic_verdict (uses)    # the arm's terminal plan_review_verdict

This module owns the agent's structured-output contract and the two scripted ops. Mapping the
agent's findings onto a PASS/BLOCK verdict is the adapter ticket's work; until it lands the
verdict op returns an unsigned INDETERMINATE that carries the validated findings, so an agentic
review can never sign.

Import-light: the ops lazy-import the plan-review units, and the contract builder imports
pydantic inside its body, so importing this module only runs the registration decorators.
"""

from __future__ import annotations

from typing import Any

from rebar.llm.workflow.executor import StepContext, register_step

#: The agent's structured-output contract name (``RunRequest.output_schema``).
FINDINGS_CONTRACT = "plan_review_agentic_findings"
#: The four severities the agent may claim; only ``blocker`` blocks (ADR 0123).
SEVERITIES = ("blocker", "major", "minor", "trivial")
#: The finding id of the interim INDETERMINATE the verdict op returns.
ADAPTER_PENDING_ID = "agentic-adapter-pending"
#: The model CLASS the agent step runs on (resolved through the operator's class table).
AGENT_MODEL_CLASS = "frontier"
#: The DET floor criteria (P1-P11) the Layer-1 floor already decided before the agent runs.
_DET_EXEC = "DET"


def _findings_model() -> type:
    """The agent's output: ``{findings: [...]}``. No field asks for written reasoning — Bedrock
    Opus 5.5 declines requests whose output contract does (the agent reasons in thinking)."""
    from typing import Literal

    from pydantic import BaseModel, Field

    class AgenticFinding(BaseModel):
        id: str = Field(description="A short id unique within this review, e.g. F1.")
        title: str = Field(description="A short handle for the defect.")
        description: str = Field(description="The claim, stated so it can be judged.")
        severity: Literal["blocker", "major", "minor", "trivial"] = Field(
            description="blocker only when the defect would stop the plan from shipping."
        )
        evidence: str = Field(description="The code or plan text that makes the claim true.")
        suggested_fix: str = Field(description="The concrete change to the plan.")
        criteria: list[str] = Field(
            default_factory=list,
            description="Ids of the listed review criteria this finding falls under; may be empty.",
        )

    class AgenticFindings(BaseModel):
        findings: list[AgenticFinding] = Field(
            default_factory=list, description="Every distinct defect found; empty when none."
        )

    return AgenticFindings


def register_contracts() -> None:
    """Register the agent's output contract (idempotent) before any agentic call runs."""
    from rebar.llm import contracts

    contracts.register_contract(FINDINGS_CONTRACT, _findings_model)


register_contracts()


def criteria_context(repo_root: str | None = None) -> str:
    """One ``id — name`` line per LLM-judged criterion in the effective registry. The DET floor
    criteria (already decided before the agent runs) and nameless synthetic entries such as
    ``prerequisite-consistency`` are left out."""
    from . import registry

    lines = [
        f"- {cid} — {crit['name']}"
        for cid, crit in sorted(registry.by_id(repo_root).items())
        if crit.get("name") and crit.get("exec") != _DET_EXEC
    ]
    return "\n".join(lines)


def render_plan(subject_plan: str, prerequisites: list[dict[str, Any]]) -> str:
    """The plan the agent reviews: the subject plan, then its direct prerequisites in the same
    stable ``<prerequisite id=...>`` blocks the pipeline's focused finder reads."""
    from .prerequisites import render_blocks

    if not prerequisites:
        return subject_plan
    return (
        f"{subject_plan}\n\n## Direct prerequisites (authoritative, read-only context)\n\n"
        f"{render_blocks(prerequisites)}"
    )


@register_step(
    "plan_review_agentic_inputs",
    input_schema="plan_review_agentic_inputs_input",
    output_schema="plan_review_agentic_inputs_output",
    description=(
        "Render the agentic plan-review prompt's inputs: `plan` (the subject plan plus its "
        "direct-prerequisite blocks) and `criteria` (one `id — name` line per LLM-judged "
        "registry criterion). Deterministic; no LLM call."
    ),
)
def plan_review_agentic_inputs(ctx: StepContext) -> dict[str, Any]:
    """Render ``plan`` and ``criteria`` for the ``plan-review-agentic`` prompt."""
    subject_plan = str(ctx.inputs.get("subject_plan") or "")
    prerequisites = [p for p in (ctx.inputs.get("prerequisites") or []) if isinstance(p, dict)]
    return {
        "plan": render_plan(subject_plan, prerequisites),
        "criteria": criteria_context(ctx.repo_root),
    }


@register_step(
    "plan_review_agentic_verdict",
    input_schema="plan_review_agentic_verdict_input",
    output_schema="plan_review_verdict",
    description=(
        "The agentic arm's terminal plan_review_verdict. Until the verdict adapter lands it is "
        "an unsigned INDETERMINATE (finding `agentic-adapter-pending`) carrying the agent's "
        "validated findings on `coverage.agentic_findings`, so an agentic review never signs."
    ),
)
def plan_review_agentic_verdict(ctx: StepContext) -> dict[str, Any]:
    """Wrap the agent's findings in an unsigned INDETERMINATE plan_review_verdict."""
    from dataclasses import replace

    from rebar.llm.config import resolve_gate_config, resolve_model

    from .claimability import indeterminate_verdict

    gate_cfg = resolve_gate_config(ctx.repo_root)
    # The verdict's `model` names the class the agent step ran on, not the verifier class the
    # gate's shared config carries.
    agent_cfg = replace(gate_cfg, model=resolve_model(gate_cfg, step=AGENT_MODEL_CLASS))

    findings = [f for f in (ctx.inputs.get("findings") or []) if isinstance(f, dict)]
    ticket_id = str(ctx.inputs.get("ticket_id") or ctx.target_ticket or "")
    return indeterminate_verdict(
        ticket_id,
        ticket_type=str(ctx.inputs.get("ticket_type") or ""),
        finding={
            "id": ADAPTER_PENDING_ID,
            "reason": (
                f"the agentic engine reviewed the plan and returned {len(findings)} finding(s), "
                "but its verdict adapter is not available yet, so no PASS/BLOCK was decided"
            ),
        },
        coverage_extra={"engine": "agentic", "llm_ran": True, "agentic_findings": findings},
        signature_reason=ADAPTER_PENDING_ID,
        remediation=(
            "Review the findings on coverage.agentic_findings, or rerun with engine "
            "'pipeline' to earn a signable verdict; no plan-review attestation was signed."
        ),
        cfg=agent_cfg,
    )
