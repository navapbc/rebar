"""The plan-review Pass-1 finder machinery (extracted from :mod:`.orchestrator`).

Pass-1 is the facet-chunked single-turn finder stage + one agent per code-grounding
criterion (the SIZE ladder: batch → one-criterion-per-call → escalate model → P8
too-big failure finding; content is never chunked), plus the dedicated container
(G3/G4) per-child loop and the ISF (in-session-failure) finder fed the linked
session log. :func:`run_pass1` builds and returns the raw findings list (up to and
including ISF); the too-big/shed routing and Pass-2/Pass-3 stay in
:mod:`.orchestrator`.

This module is shared by the bespoke orchestrator and, later, the production
batch-runner (epic B / story B1). It must NOT import from :mod:`.orchestrator`
(that would create an import cycle).
"""

from __future__ import annotations

import hashlib
import json
import logging
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from rebar.llm.config import LLMConfig
from rebar.llm.errors import LLMUnavailableError
from rebar.llm.runner import Runner

from . import det_floor, passes, registry, sizing
from .det_floor import PlanContext

logger = logging.getLogger(__name__)

# Private aliases for the sizing helpers the moved code calls (preserve the
# historical call sites verbatim).
_pass1_with_ladder = sizing.pass1_with_ladder
_shed_to_budget = sizing.shed_to_budget


# Container criteria (parent + one child at a time); handled by the dedicated
# per-child loop, never the normal agent path.
CONTAINER_CRITERIA = ("G3", "G4")


def _run_container(
    ctx: PlanContext,
    cfg: LLMConfig,
    runner: Runner,
    container: list[dict],
    coverage: dict[str, Any],
) -> list[dict[str, Any]]:
    """Run the container criteria (G3/G4) as (parent + ONE child) pairings, both
    whole, and aggregate. A pairing too big for the largest window is a failure
    finding (reduce the ticket), not a skip. The complete sibling roster is fed so
    absence findings can be cross-checked against ALL siblings before they stand."""
    roster = "\n".join(f"- {c.get('ticket_id')}: {c.get('title', '')}" for c in ctx.children)
    budget = (
        int(ctx.largest_window_tokens * det_floor.P8_HEADROOM) - det_floor.P8_OUTPUT_RESERVE_TOKENS
    )
    parent_tokens = det_floor.est_tokens(ctx.plan_text)
    out: list[dict[str, Any]] = []
    pairings = 0
    for crit in container:
        for child in ctx.children:
            pair_tokens = parent_tokens + det_floor.est_tokens(
                f"{child.get('title', '')}\n{child.get('description', '')}"
            )
            if pair_tokens > budget:
                out.append(
                    {
                        "finding": (
                            f"The (parent + child {child.get('ticket_id')}) pairing is too big to "
                            f"review together for {crit['id']} (~{pair_tokens} tokens > budget)."
                        ),
                        "criteria": [crit["id"]],
                        "location": f"child {child.get('ticket_id')}",
                        "evidence": [f"parent+child ~{pair_tokens} tokens exceeds ~{budget}"],
                        "scenarios": [],
                        "impact": "Container coverage/consistency cannot be checked at this size.",
                        "checklist_item": (
                            f"- [ ] Reduce the parent or child {child.get('ticket_id')} so they "
                            "review together."
                        ),
                        "suggested_fix": "Decompose the oversized ticket(s).",
                        "tier": "DET",
                    }
                )
                continue
            try:
                out.extend(
                    passes.pass1_container(
                        runner,
                        cfg,
                        parent_plan=ctx.plan_text,
                        child=child,
                        criterion=crit,
                        sibling_roster=roster,
                    )
                )
            except Exception:  # noqa: BLE001 — a failed P5 pairing drops its findings, never aborts the review
                pass
            pairings += 1
    coverage["container"] = {
        "criteria": [c["id"] for c in container],
        "children": len(ctx.children),
        "pairings_evaluated": pairings,
    }
    return out


def _ticket_graph_blob(ctx: PlanContext) -> str:
    """A compact, PRE-RESOLVED ticket-graph context for ISF (parent / children /
    dependency links) — resolved by the orchestrator from the already-loaded state
    and INJECTED (ISF is single-turn, not agentic, so it never fetches the graph
    itself). Children may cover an expressed requirement, so ISF needs the graph to
    avoid false 'silently dropped' findings."""
    lines: list[str] = []
    parent = ctx.state.get("parent_id")
    if parent:
        lines.append(f"parent: {parent}")
    if ctx.children:
        lines.append("children:")
        lines.extend(
            f"  - {c.get('ticket_id')}: {(c.get('title') or '')[:80]}" for c in ctx.children
        )
    deps = ctx.state.get("deps", []) or []
    if deps:
        lines.append("links:")
        lines.extend(
            f"  - {d.get('relation')} -> {d.get('target_id')}" for d in deps if d.get("target_id")
        )
    return "\n".join(lines)


def _linked_session_log(ctx: PlanContext, cfg: LLMConfig, runner) -> tuple[str | None, bool]:
    """The text of the ticket's linked SESSION LOG(s) for the ISF criterion, and
    whether it was summarized to fit the window. Returns ``(None, False)`` when no
    session log is linked (ISF then does not run). Best-effort: any read error →
    ``(None, False)`` (ISF is skipped, never crashes the review)."""
    import rebar

    bodies: list[str] = []
    try:
        for dep in ctx.state.get("deps", []) or []:
            tgt = dep.get("target_id")
            if not tgt:
                continue
            try:
                log = rebar.show_ticket(tgt, repo_root=ctx.repo_root)
            except Exception:  # noqa: BLE001 — per-dep best-effort session-log fetch; skip unreadable deps
                continue
            if log.get("ticket_type") == "session_log":
                bodies.append(f"# {log.get('title', '')}\n{log.get('description', '')}")
    except Exception:  # noqa: BLE001 — ISF is supporting context, not the plan; broad-but-logged below, ISF skipped
        # ISF is supporting context, not the plan — any read error skips it, but log
        # the failure so a silently-missing ISF context is observable.
        logger.warning("ISF session-log gather failed; skipping ISF", exc_info=True)
        return (None, False)
    if not bodies:
        return (None, False)
    text = "\n\n".join(bodies)
    budget = (
        int(ctx.largest_window_tokens * det_floor.P8_HEADROOM) - det_floor.P8_OUTPUT_RESERVE_TOKENS
    )
    if det_floor.est_tokens(text) <= budget:
        return (text, False)
    # Oversized: summarize (the supporting context only — never the plan).
    try:
        return (passes.summarize_for_isf(runner, cfg, log_text=text), True)
    except Exception:  # noqa: BLE001 — ISF summarization is best-effort; broad-but-logged below, ISF skipped
        # Summarization failure → ISF runs without the oversized log; log it (floor).
        logger.warning("ISF summarization failed; skipping ISF", exc_info=True)
        return (None, False)


def _ticket_size(ctx: PlanContext) -> str:
    if ctx.level == "epic" or ctx.has_children:
        return "epic"
    if det_floor.est_tokens(ctx.plan_text) > 8000:
        return "large"
    return "moderate"


def run_pass1(
    ctx: PlanContext,
    cfg: LLMConfig,
    runner: Runner,
    single: list[dict],
    agent: list[dict],
    coverage: dict[str, Any],
) -> list[dict[str, Any]]:
    """Pass-1 (parallel single-turn chunks + per-criterion agent finders + container
    + ISF). Returns the raw findings list (the too-big/shed routing + Pass-2/Pass-3
    are applied by the orchestrator)."""
    plan = ctx.plan_text
    size = _ticket_size(ctx)
    # Container criteria (G3/G4) run via the dedicated per-child loop, not the normal
    # agent path — pull them out of `agent`.
    container = [c for c in agent if c["id"] in CONTAINER_CRITERIA]
    agent = [c for c in agent if c["id"] not in CONTAINER_CRITERIA]
    chunks = registry.chunk_by_facet(single, model=cfg.model, ticket_size=size)
    coverage["chunks"] = len(chunks)

    # ── per-plan budget cap: shed the lowest-priority AGENT/overlay criteria first ──
    # Single-turn chunks (cheap) + DET (free) always run; if the projected spend
    # exceeds the cap, shed AGENT/overlay criteria (the 85× calls) lowest-priority
    # first — overlays before core code-grounding — recording each as INDETERMINATE.
    agent, container, shed = _shed_to_budget(ctx, chunks, agent, container, coverage)
    budget_indeterminate = [
        {
            "finding": f"Criterion {c['id']} was not evaluated: per-plan budget cap reached.",
            "criteria": [c["id"]],
            "location": "(budget cap)",
            "evidence": ["AGENT/overlay criterion shed to stay within the per-plan budget cap"],
            "scenarios": [],
            "impact": "This criterion is unverified for this review (non-blocking INDETERMINATE).",
            "decision": "indeterminate",
            "reason": "budget-cap-shed",
            "severity": "none",
            "validity": 0.0,
            "impact_score": 0.0,
            "priority": 0.0,
            "tier": c.get("_tier", "AGENT"),
            "_shed": True,
        }
        for c in shed
    ]

    findings: list[dict[str, Any]] = list(budget_indeterminate)
    ladder_events: list[str] = []
    # Chunk-atomic CHECKPOINTING: resume completed Pass-1 chunks from a prior run
    # (keyed by the ticket's MATERIAL fingerprint, so an edit invalidates the cache),
    # and persist each chunk's result atomically as it completes.
    material = material_fingerprint(ctx)
    resumed = 0

    def _chunk(chunk: list[dict], agentic: bool) -> list[dict[str, Any]]:
        nonlocal resumed
        cached = sizing.load_checkpoint(ctx, material, chunk, cfg.model, agentic)
        if cached is not None:
            resumed += 1
            return cached
        out = _pass1_with_ladder(runner, cfg, plan, chunk, agentic, ladder_events)
        sizing.save_checkpoint(ctx, material, chunk, cfg.model, agentic, out)
        return out

    max_workers = max(1, min(6, len(chunks) + len(agent)))
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        st_futs = [ex.submit(_chunk, ch, False) for ch in chunks]
        ag_futs = [ex.submit(_chunk, [c], True) for c in agent]
        for fu in st_futs + ag_futs:
            try:
                findings.extend(fu.result() or [])
            except LLMUnavailableError:
                # SYSTEMIC failure (auth / missing key / connection / rate-limit) affects
                # the whole tier — surface it, never silently drop (fuel-posse-ball). The
                # run_review caller turns this into an INDETERMINATE, unsigned verdict.
                raise
            except Exception:  # noqa: BLE001 — a NON-systemic per-chunk failure: drop its findings, never aborts
                coverage["chunk_errors"] = coverage.get("chunk_errors", 0) + 1
                logger.warning(
                    "a plan-review chunk failed (non-systemic); findings dropped", exc_info=True
                )
    if ladder_events:
        coverage["size_ladder"] = ladder_events
    coverage["checkpoint"] = {"chunks_resumed": resumed, "chunks_total": len(chunks) + len(agent)}

    # Container criteria (G3 child coverage / G4 child consistency): evaluate
    # (parent + ONE child) per call, both whole, and AGGREGATE — never the whole
    # child set in one call. A too-big (parent+child) pairing is a failure finding
    # (reduce the ticket), not a silent skip. Absence findings are cross-checked
    # against the COMPLETE sibling roster (fed to the finder + re-verified in Pass-2).
    if container and ctx.has_children:
        findings.extend(_run_container(ctx, cfg, runner, container, coverage))

    # ISF (child 681b): fed the LINKED SESSION LOG (not a rubric chunk), single-turn,
    # fires ONLY when a session log is linked. Oversized logs fall back to a SUMMARY
    # (recorded; findings carry reduced confidence). The plan is never summarized.
    log_text, summarized = _linked_session_log(ctx, cfg, runner)
    if log_text:
        coverage["isf"] = {"ran": True, "summarized": summarized}
        try:
            findings.extend(
                passes.pass1_isf(
                    runner,
                    cfg,
                    plan=plan,
                    session_log_text=log_text,
                    ticket_graph=_ticket_graph_blob(ctx),
                    summarized=summarized,
                )
            )
        except Exception as exc:  # noqa: BLE001 — ISF pass is best-effort; broad-but-logged + recorded in coverage, never blocks the verdict
            # ISF (in-session-failure) pass is best-effort; record in-band + log (floor).
            logger.warning("ISF pass failed; verdict emitted without ISF findings", exc_info=True)
            coverage["isf"]["error"] = str(exc)
    else:
        coverage["isf"] = {"ran": False, "reason": "no linked session_log"}

    return findings


def material_fingerprint(ctx: PlanContext) -> str:
    """A hash of the ticket's MATERIAL plan content (description / AC / file_impact /
    decomposition) — bound into the attestation so a material edit invalidates the
    signature. Tags/comments/links/assignee are NOT material (excluded)."""
    basis = {
        "ticket_id": ctx.ticket_id,
        "description": ctx.description,
        "file_impact": ctx.state.get("file_impact") or [],
        "children": sorted(c.get("ticket_id", "") for c in ctx.children),
    }
    blob = json.dumps(basis, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]
