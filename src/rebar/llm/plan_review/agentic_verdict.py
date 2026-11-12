"""The agentic engine's verdict adapter (ADR 0123): map the agent's findings onto the pipeline's
finding and ``plan_review_verdict`` shape, so every downstream reader (claim gate,
``review-plan --status``, ``sign-review``, the MCP status tools, the sidecar) reads one contract.

The verdict is built with the pipeline's own two functions:
``orchestrator.partition_findings`` (id minting, contentless/duplicate narrowing, the advisory
cap) and ``orchestrator.finalize_verdict`` (the verdict string and counts). The decision comes
from severity alone — only ``blocker`` blocks; ``major``, ``minor`` and ``trivial`` are advisory
(operator decision on the epic, 2026-11-09) — and no criterion posture changes it. Agentic
findings carry no ``priority`` (ADR 0123), so the stable advisory sort keeps the agent's order.

A failed agent run never reaches this module: it fails the workflow step and degrades through
``plan_review_recovery`` to an unsigned INDETERMINATE.
"""

from __future__ import annotations

from typing import Any

#: The ``tier`` stamped on every adapted finding (DET floor findings carry ``"DET"``).
TIER = "AGENTIC"
#: The only severity that blocks (ADR 0123).
BLOCKING_SEVERITY = "blocker"


def _criteria(raw: object, known: set[str]) -> list[str]:
    """The finding's criterion ids that exist in the registry, in the agent's order."""
    if not isinstance(raw, list):
        return []
    return [c for c in raw if isinstance(c, str) and c in known]


def map_finding(finding: dict[str, Any], known_criteria: set[str]) -> dict[str, Any]:
    """One agent finding as a pipeline finding: text, evidence, fix, criteria, decision."""
    title = str(finding.get("title") or "").strip()
    description = str(finding.get("description") or "").strip()
    text = f"{title}: {description}" if title and description else (title or description)
    evidence = str(finding.get("evidence") or "").strip()
    severity = str(finding.get("severity") or "")
    return {
        "finding": text,
        "criteria": _criteria(finding.get("criteria"), known_criteria),
        "evidence": [evidence] if evidence else [],
        "suggested_fix": str(finding.get("suggested_fix") or ""),
        "severity": severity,
        "decision": "block" if severity == BLOCKING_SEVERITY else "advisory",
        "tier": TIER,
        "agentic_id": str(finding.get("id") or ""),
    }


def adapt_findings(
    findings: list[dict[str, Any]],
    *,
    ticket_id: str,
    ticket_type: str,
    det_blocking: list[dict[str, Any]],
    det_advisory: list[dict[str, Any]],
    det_coverage: dict[str, Any],
    hierarchy_incomplete: bool,
    hierarchy_incomplete_detail: list[str],
    runner_name: str | None,
    model: str | None,
    provider_provenance: dict[str, Any] | None,
    repo_root: str | None = None,
) -> dict[str, Any]:
    """The agentic arm's terminal ``plan_review_verdict``: PASS with no blocker, BLOCK with
    any, INDETERMINATE when the ticket hierarchy failed to load — as for the pipeline."""
    from . import orchestrator, registry
    from .det_floor import PlanContext

    known = set(registry.by_id(repo_root))
    mapped = [map_finding(f, known) for f in findings if isinstance(f, dict)]
    parts = orchestrator.partition_findings(
        list(det_blocking),
        list(det_advisory),
        mapped,
        advisory_cap=orchestrator.DEFAULT_ADVISORY_CAP,
    )
    coverage = {
        "det": det_coverage,
        "llm_ran": True,
        "engine": "agentic",
        "hierarchy_incomplete": bool(hierarchy_incomplete),
        "hierarchy_incomplete_detail": list(hierarchy_incomplete_detail),
    }
    pctx = PlanContext(ticket_id=ticket_id, ticket_type=ticket_type, title="", description="")
    return orchestrator.finalize_verdict(
        pctx,
        parts,
        coaching=[],
        coverage=coverage,
        runner_name=runner_name,
        model=model,
        provider_provenance=provider_provenance,
    )
