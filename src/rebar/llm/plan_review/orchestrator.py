"""The plan-review orchestrator (child ca03) — the engine that runs the gate.

It owns the generic flow, reusing rebar.llm extension points (the runner, the
prompt/contract model) and the sibling modules (:mod:`.det_floor`, :mod:`.passes`,
:mod:`.registry`):

1. **Assemble** the whole ticket context (plan + children) from rebar's own reads
   — content is ALWAYS whole; never truncated, never content-chunked.
2. **DET tier** — run the deterministic floor (P1–P8) via the code executor; its
   blocking findings (P1/P5-cycle/P8) are the gate's only default blocks.
3. **Route** the LLM criteria: ``applies_at`` proportionate scrutiny + overlay
   triggering; only the code-grounding set greps the codebase.
4. **Pass 1** — facet-chunked single-turn finders + one agent per code-grounding
   criterion (the SIZE ladder: batch → one-criterion-per-call → escalate model →
   P8 too-big failure finding; content never chunked).
5. **Pass 2** — one aggregate verifier pass.
6. **Pass 3** — deterministic decision per finding; **mint** the stable per-finding
   ``id`` (content fingerprint — the orchestrator is its SOLE owner).
7. **Cap** — surface the top-N advisory findings by priority; overflow → sidecar;
   blocking findings are EXEMPT from the cap (all returned).
8. **Pass 4** — affirmative coach over the surviving advisory findings.
9. **Assemble** the ``plan_review_verdict`` + the coverage record (for the
   attestation) + the per-criterion sidecar payload.
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from rebar.llm.config import LLMConfig
from rebar.llm.runner import Runner, get_runner

from . import det_floor, passes, registry
from .det_floor import PlanContext

logger = logging.getLogger(__name__)

# Advisory surfacing cap (config-overridable; owned by child 55de). Blocking
# findings are EXEMPT — the cap can never weaken the block decision.
DEFAULT_ADVISORY_CAP = 10

# The size/budget/ladder/checkpoint cluster lives in :mod:`.sizing`; re-export the
# names this module + the tests use (backward-compatible public surface). Private
# aliases (``_centrality`` etc.) preserve the historical call sites.
from . import sizing  # noqa: E402
from .sizing import (  # noqa: E402
    largest_window_tokens,
)

_centrality = sizing.centrality
_is_context_limit_error = sizing.is_context_limit_error
_models_at_or_above = sizing.models_at_or_above
_pass1_with_ladder = sizing.pass1_with_ladder
_shed_to_budget = sizing.shed_to_budget


# Pass-4 move registry (moves 1-9,11,12 with LOCKED templates; project-extensible
# via .rebar later — child 75a9). The LLM picks the move + names a {subject}; the
# prose is rendered deterministically from these templates (it never authors prose).
MOVE_REGISTRY: dict[str, dict[str, str]] = {
    "1": {
        "name": "spike",
        "template": "Consider a short spike to de-risk {subject} before committing the plan.",
    },
    "2": {
        "name": "prior-art research",
        "template": "Research prior art / OSS for {subject} before building it custom.",
    },
    "3": {
        "name": "pre-mortem",
        "template": "Run a quick pre-mortem on {subject}: how could this plan fail?",
    },
    "4": {
        "name": "riskiest-assumption test",
        "template": "Test the riskiest assumption behind {subject} first.",
    },
    "5": {
        "name": "weigh alternatives",
        "template": "Weigh at least one structural alternative for {subject}.",
    },
    "6": {
        "name": "specification by example",
        "template": "Pin down {subject} with a concrete worked example.",
    },
    "7": {
        "name": "thin vertical slice",
        "template": "Prove {subject} end-to-end with a thin vertical slice first.",
    },
    "8": {
        "name": "ADR / one-way-door",
        "template": "Record an ADR for {subject} — it reads like a one-way door.",
    },
    "9": {
        "name": "plan the verification",
        "template": "Plan how {subject} will be verified before implementing it.",
    },
    "11": {
        "name": "propagate to children",
        "template": "Propagate the revision for {subject} to the child tickets.",
    },
    "12": {
        "name": "generalize the finding",
        "template": "Generalize {subject} across the rest of the work.",
    },
}


def load_move_registry(repo_root=None) -> dict[str, dict[str, str]]:
    """The Pass-4 move registry: the built-in moves PLUS project extensions from
    ``.rebar/plan_review_moves.json`` (a ``{move_id: {name, template}}`` map; a
    project entry adds a new move or overrides a built-in by id). The template must
    contain a single ``{subject}`` placeholder — the LLM never authors prose.
    Best-effort: a missing/malformed file → built-ins only (never crashes the review)."""
    moves = {mid: dict(m) for mid, m in MOVE_REGISTRY.items()}
    if not repo_root:
        return moves
    try:
        path = Path(repo_root) / ".rebar" / "plan_review_moves.json"
        if path.is_file():
            extra = json.loads(path.read_text(encoding="utf-8"))
            for mid, m in (extra or {}).items():
                if isinstance(m, dict) and m.get("name") and "{subject}" in str(m.get("template")):
                    moves[str(mid)] = {"name": m["name"], "template": m["template"]}
    except Exception:  # noqa: BLE001 — project move file is best-effort
        pass
    return moves


# ── context assembly ─────────────────────────────────────────────────────────────
def assemble_context(
    ticket_id: str, *, repo_root=None, cfg: LLMConfig | None = None
) -> PlanContext:
    """Build the whole-ticket :class:`PlanContext` from rebar reads (ticket + its
    direct children, each whole). The largest context window is taken from the
    model ladder for P8's budget."""
    import rebar

    state = rebar.show_ticket(ticket_id, repo_root=repo_root)
    canonical = state.get("ticket_id", ticket_id)
    children: list[dict[str, Any]] = []
    try:
        listed = rebar.list_tickets(parent=canonical, repo_root=repo_root) or []
    except Exception:  # noqa: BLE001 — children enumeration degrades P5/P8 if it fails; broad-but-logged below, review continues
        # Failing to enumerate children degrades P5/P8 coverage — a real signal, logged.
        logger.warning("could not list children of %s; reviewing without", canonical, exc_info=True)
        listed = []
    for c in listed:
        cid = c.get("ticket_id")
        try:  # fetch full child state (deps + file_impact) for P5/P8
            children.append(rebar.show_ticket(cid, repo_root=repo_root))
        except Exception:  # noqa: BLE001 — per-child best-effort full-state fetch; fall back to the summary
            children.append(c)
    return PlanContext(
        ticket_id=canonical,
        ticket_type=state.get("ticket_type", ""),
        title=state.get("title", ""),
        description=state.get("description", ""),
        state=state,
        children=children,
        repo_root=str(repo_root) if repo_root else (cfg.repo_path if cfg else None),
        largest_window_tokens=largest_window_tokens(cfg.model if cfg else None),
        centrality=_centrality(state, children),
    )


# ── finding id (content fingerprint — orchestrator is the SOLE owner) ─────────────
def mint_finding_id(finding: dict[str, Any]) -> str:
    """A stable content fingerprint for a finding (the caller-visible ``id`` the
    coach + sidecar reference, never restate). Keyed on the finding text + its
    criteria so the same defect across runs gets the same id."""
    basis = finding.get("finding", "") + "|" + ",".join(sorted(finding.get("criteria", [])))
    return "f" + hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]


# ── routing ──────────────────────────────────────────────────────────────────────
def route_criteria(ctx: PlanContext) -> tuple[list[dict], list[dict]]:
    """Filter + split the LLM criteria into (single-turn/2-step, agent-tier),
    applying proportionate scrutiny and overlay triggering. Returns the two lists."""
    plan = ctx.plan_text
    triggers = registry.overlay_triggers(plan)
    single, agent = [], []
    for c in registry.load_criteria():
        cid = c["id"]
        # ISF is special: it is fed the linked SESSION LOG (not the rubric chunk) and
        # fires only when a session log is linked — handled separately in _run_passes,
        # so it never enters the normal single/agent routing.
        if cid == "ISF":
            continue
        if not registry.applies(
            c,
            level=ctx.level,
            has_children=ctx.has_children,
            ticket_type=ctx.ticket_type,
            plan=plan,
        ):
            continue
        # Deterministic overlays only run when triggered; LLM-routed overlays
        # (those absent from the deterministic trigger map) always enter the finder,
        # which decides applicability (PASS not-applicable is cheap).
        if registry.is_overlay(cid) and cid in triggers and not triggers[cid]:
            continue
        if registry.exec_tier(c) == "AGENT":
            agent.append(c)
        else:
            single.append(c)
    return single, agent


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


# ── the review ───────────────────────────────────────────────────────────────────
def run_review(
    ctx: PlanContext,
    cfg: LLMConfig,
    *,
    runner: Runner | None = None,
    advisory_cap: int = DEFAULT_ADVISORY_CAP,
    run_coach: bool = True,
) -> dict[str, Any]:
    """Run the full gate on an assembled context and return a ``plan_review_verdict``."""
    # Bugs are exempt from this gate (different shape; follow-on 3e50).
    if ctx.ticket_type == "bug":
        return _exempt_verdict(ctx, reason="bug tickets are exempt from the plan-review gate")
    if ctx.ticket_type == "session_log":
        return _exempt_verdict(ctx, reason="session_log tickets are gate-exempt")

    # Per-pass LATENCY capture (child db7b AC5): wall-clock timing for the DET tier,
    # the out-of-band LLM review, and the total — recorded on the sidecar so
    # latency/cost targets are refined PASSIVELY from dogfood data (no upfront
    # benchmark). The CLAIM-path check is structurally LLM/network-free (092b), so its
    # cost is recorded as a constant marker, not timed here.
    _t_total = time.monotonic()

    # ── DET tier (exec=DET) ──────────────────────────────────────────────────────
    _t_det = time.monotonic()
    det_results = det_floor.run_det_floor(ctx)
    det_ms = round((time.monotonic() - _t_det) * 1000, 1)
    det_blocks = det_floor.det_blocking_findings(det_results)
    det_advisories = det_floor.det_advisory_findings(det_results)
    coverage: dict[str, Any] = {"det": det_floor.det_coverage(det_results)}

    # If P8 says the ticket is too big to review at all, STOP before the LLM tiers —
    # any LLM review would see a plan that doesn't fit (the size ladder's terminal).
    p8_too_big = any(r.id == "P8" and r.blocked for r in det_results)

    findings: list[dict[str, Any]] = []
    llm_ran = False
    llm_ms = 0.0
    if not p8_too_big:
        single, agent = route_criteria(ctx)
        coverage["routing"] = {
            "single_turn": [c["id"] for c in single],
            "agent_tier": [c["id"] for c in agent],
        }
        runner_sel = runner or get_runner(cfg)
        try:
            runner_sel.preflight()
            _t_llm = time.monotonic()
            findings = _run_passes(ctx, cfg, runner_sel, single, agent, coverage)
            llm_ms = round((time.monotonic() - _t_llm) * 1000, 1)
            llm_ran = True
        except Exception as exc:  # noqa: BLE001 — LLM unavailable → fail-open to DET-only review; broad-but-logged + recorded in coverage
            # Fail-open to a DET-only review, but record the error in-band (coverage)
            # AND on the logger so a degraded review is observable to the operator.
            logger.warning("LLM passes failed; falling back to DET-only review", exc_info=True)
            coverage["llm_error"] = str(exc)
            coverage["llm_ran"] = False
    coverage.setdefault("llm_ran", llm_ran)

    # ── assemble: ids, decisions already attached; merge DET findings ─────────────
    blocking: list[dict[str, Any]] = []
    advisory: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    indeterminate: list[dict[str, Any]] = []

    for f in det_blocks:
        f = {
            **f,
            "id": mint_finding_id(f),
            "decision": "block",
            "severity": "critical",
            "priority": 1.0,
            "validity": 1.0,
            "impact": 1.0,
            "tier": "DET",
        }
        blocking.append(f)
    for f in det_advisories:
        f = {
            **f,
            "id": mint_finding_id(f),
            "decision": "advisory",
            "severity": "minor",
            "priority": 0.4,
            "validity": 1.0,
            "impact": 0.4,
            "tier": "DET",
        }
        advisory.append(f)

    for f in findings:
        f = {**f, "id": mint_finding_id(f)}
        d = f.get("decision")
        if d == "block":
            blocking.append(f)
        elif d == "advisory":
            advisory.append(f)
        elif d == "indeterminate":
            indeterminate.append(f)
        else:
            dropped.append(f)

    # ── cap (advisory only; blocking EXEMPT) ──────────────────────────────────────
    # Guard the load-bearing invariant: the cap must NEVER see a blocking finding
    # (all blocking findings are returned regardless of N). A refactor that leaked
    # one into `advisory` would otherwise silently drop a block — fail loud instead.
    assert not any(f.get("decision") == "block" for f in advisory), (
        "blocking finding leaked into the advisory cap"
    )
    advisory.sort(key=lambda f: -float(f.get("priority", 0.0)))
    surfaced = advisory[:advisory_cap]
    overflow = advisory[advisory_cap:]

    # ── Pass 4 coach over the surviving advisory findings ─────────────────────────
    coaching: list[dict[str, Any]] = []
    if run_coach and surfaced and llm_ran:
        try:
            coaching = passes.pass4_coach(
                runner or get_runner(cfg),
                cfg,
                plan=ctx.plan_text,
                surviving=surfaced,
                move_registry=load_move_registry(ctx.repo_root),
            )
        except Exception as exc:  # noqa: BLE001 — coaching is advisory polish; broad-but-logged + recorded in coverage, never blocks the verdict
            # Coaching is advisory polish — its failure never blocks the verdict, but
            # record it in-band and on the logger (floor).
            logger.warning("pass-4 coaching failed; verdict emitted without it", exc_info=True)
            coverage["coach_error"] = str(exc)

    verdict = (
        "BLOCK" if blocking else ("INDETERMINATE" if indeterminate and not surfaced else "PASS")
    )
    coverage["counts"] = {
        "blocking": len(blocking),
        "advisory_surfaced": len(surfaced),
        "advisory_overflow": len(overflow),
        "dropped": len(dropped),
        "indeterminate": len(indeterminate),
    }
    # Per-pass latency + a cost proxy (LLM-call count) for passive refinement (db7b AC5).
    coverage["metrics"] = {
        "det_ms": det_ms,
        "llm_ms": llm_ms,
        "total_ms": round((time.monotonic() - _t_total) * 1000, 1),
        "llm_calls": int(coverage.get("chunks", 0))
        + len(coverage.get("routing", {}).get("agent_tier", []))
        + (1 if llm_ran and (surfaced or overflow) else 0),
        "claim_path": "no-llm/no-network (structural; the fast claim check is a local HMAC verify)",
    }
    return {
        "verdict": verdict,
        "ticket_id": ctx.ticket_id,
        "ticket_type": ctx.ticket_type,
        "blocking": blocking,
        "advisory": surfaced,
        "coaching": coaching,
        "overflow": overflow,  # sidecar-only (not surfaced to the agent)
        "indeterminate": indeterminate,
        "dropped": dropped,  # sidecar-only
        "coverage": coverage,
        "runner": (runner.name if runner else cfg.runner),
        "model": cfg.model,
    }


def _run_passes(
    ctx: PlanContext,
    cfg: LLMConfig,
    runner: Runner,
    single: list[dict],
    agent: list[dict],
    coverage: dict[str, Any],
) -> list[dict[str, Any]]:
    """Pass-1 (parallel single-turn chunks + per-criterion agent finders) → Pass-2
    aggregate verify → Pass-3 deterministic decide. Returns findings with decisions."""
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
            except Exception:  # noqa: BLE001 — a failed chunk drops its findings, never aborts the review
                pass
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

    # The size-ladder's "too big even at the largest model" findings are DET-style
    # BLOCKS (the P8 outcome discovered at runtime) — route them straight to the
    # blocking set with a fixed verdict; they do NOT go through Pass-2/3.
    too_big = [
        {
            **f,
            "decision": "block",
            "severity": "critical",
            "priority": 1.0,
            "validity": 1.0,
            "impact": 1.0,
        }
        for f in findings
        if f.get("_too_big")
    ]
    # Budget-shed criteria are pre-decided INDETERMINATE (non-blocking) and bypass
    # Pass-2/3 — there is nothing to verify (they did not run).
    shed_indeterminate = [f for f in findings if f.get("_shed")]
    findings = [f for f in findings if not f.get("_too_big") and not f.get("_shed")]

    # Pass 2: one aggregate verification pass (agentic if any code-grounded finding).
    grounded = any(
        any(c in registry.CODEBASE_GROUNDED for c in f.get("criteria", [])) for f in findings
    )
    v_cfg = passes.verifier_cfg(cfg)
    verifs = passes.pass2_verify(runner, v_cfg, plan=plan, findings=findings, agentic=grounded)

    # Pass 3: deterministic decision + thresholds per criterion.
    crit_by_id = registry.by_id()
    decided: list[dict[str, Any]] = [*too_big, *shed_indeterminate]
    for i, f in enumerate(findings):
        thresholds = [
            float(crit_by_id.get(c, {}).get("block_threshold", passes.DEFAULT_BLOCK_THRESHOLD))
            for c in f.get("criteria", [])
        ]
        blocking_enabled = any(
            str(crit_by_id.get(c, {}).get("default_posture", "advisory")).lower() == "blocking"
            for c in f.get("criteria", [])
        )
        bt = min(thresholds) if thresholds else passes.DEFAULT_BLOCK_THRESHOLD
        d = passes.pass3_decide(
            verifs.get(i), block_threshold=bt, blocking_enabled=blocking_enabled
        )
        decided.append({**f, **d, "verification": verifs.get(i), "tier": "LLM"})
    return decided


def _exempt_verdict(ctx: PlanContext, *, reason: str) -> dict[str, Any]:
    return {
        "verdict": "PASS",
        "ticket_id": ctx.ticket_id,
        "ticket_type": ctx.ticket_type,
        "blocking": [],
        "advisory": [],
        "coaching": [],
        "overflow": [],
        "indeterminate": [],
        "dropped": [],
        "coverage": {"exempt": reason},
        "runner": "exempt",
        "model": None,
    }


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
