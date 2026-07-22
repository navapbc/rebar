"""Workflow adapter for focused prerequisite verification batching.

This is a call-graph seam extracted from ``workflow_ops`` to keep both modules
agent-sized.  It consumes only the relation blocks already present in step
inputs; it never reads the ticket store.

The adapter preserves three boundaries that are easy to lose in generic map
machinery:

* focused indices remain the original flat-domain indices across every split;
* subject and prerequisite plans are repeated whole, never text-chunked; and
* packing is stable by canonical prerequisite id.

Only finding tuples are divisible.  The model therefore sees the same pairwise
evidence regardless of whether a request fits in one call or is split.  The
downstream decide adapter remains the sole owner of attribution validation and
of merging this local domain with general findings.
"""

from __future__ import annotations

import json
from typing import Any

from rebar.llm.config import resolve_gate_config
from rebar.llm.prompting import prompts
from rebar.llm.workflow.executor import StepContext, register_step

from . import passes, sizing
from .prerequisites import render_blocks


def _render(records: list[Any]) -> str:
    return "\n\n".join(
        f"PREREQUISITE {record.canonical_id}\n{record.rendered_text}\n"
        + "\n".join(
            f"[{finding['original_index']}] {json.dumps(finding, sort_keys=True)}"
            for finding in record.findings
        )
        for record in records
    )


def _split_oversized(
    record: sizing.PrerequisiteVerificationBlock,
    *,
    subject_plan: str,
    system_prompt: str,
    model: str | None,
) -> tuple[list[str], bool]:
    """Split only findings; flag exhaustion when one whole pair still cannot fit."""
    instructions: list[str] = []
    groups = [record.findings]
    while groups:
        group = groups.pop(0)
        candidate = sizing.PrerequisiteVerificationBlock(
            record.canonical_id, tuple(group), record.rendered_text
        )
        bins, _oversized = sizing.pack_prerequisite_verifier_bins(
            [candidate],
            subject_plan=subject_plan,
            system_prompt=system_prompt,
            model=model,
        )
        if bins:
            instructions.append(_render(bins[0]))
        elif len(group) > 1:
            middle = len(group) // 2
            groups[0:0] = [group[:middle], group[middle:]]
        else:
            return [], True
    return instructions, False


@register_step(
    "plan_review_prerequisite_verify_inputs",
    input_schema="plan_review_prerequisite_verify_inputs_input",
    output_schema="plan_review_prerequisite_verify_inputs_output",
    description="Build focused verifier inputs solely from the preloaded prerequisite blocks.",
)
def plan_review_prerequisite_verify_inputs(ctx: StepContext) -> dict[str, Any]:
    """Prepare stable focused Pass-2 chunks with unchanged original indices."""
    findings = list(ctx.inputs.get("findings") or [])
    blocks = list(ctx.inputs.get("prerequisites") or [])
    by_id = {str(block.get("canonical_id", "")): block for block in blocks}
    grouped: dict[str, list[dict[str, Any]]] = {}
    for index, finding in enumerate(findings):
        grouped.setdefault(str(finding.get("prerequisite_id", "")), []).append(
            {**finding, "original_index": index}
        )
    records = [
        sizing.PrerequisiteVerificationBlock(pid, tuple(group), render_blocks([by_id[pid]]))
        for pid, group in sorted(grouped.items())
        if pid in by_id
    ]
    cfg = resolve_gate_config(ctx.repo_root)
    prompt = prompts.get_prompt(passes.PASS_PREREQUISITE_VERIFIER, repo_root=cfg.repo_path)
    subject_plan = str(ctx.inputs.get("subject_plan") or "")
    system, _ = prompts.resolve_prompt(prompt, {"plan": subject_plan}, repo_root=cfg.repo_path)
    bins, oversized = sizing.pack_prerequisite_verifier_bins(
        records,
        subject_plan=subject_plan,
        system_prompt=system,
        model=cfg.model,
    )
    instructions = [_render(bin_) for bin_ in bins]
    input_too_large_ids: list[str] = []
    for record in oversized:
        split, exhausted = _split_oversized(
            record,
            subject_plan=subject_plan,
            system_prompt=system,
            model=cfg.model,
        )
        if exhausted:
            input_too_large_ids.append(record.canonical_id)
        else:
            instructions.extend(split)
    if not instructions:
        instructions = ["No focused findings; return an empty verifications array."]
    return {
        "plan": subject_plan,
        "instructions": instructions,
        "input_too_large_ids": input_too_large_ids,
    }
