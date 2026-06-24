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
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from rebar.llm.config import LLMConfig
from rebar.llm.runner import Runner, get_runner

from . import det_floor, passes, registry
from .det_floor import PlanContext

# Advisory surfacing cap (config-overridable; owned by child 55de). Blocking
# findings are EXEMPT — the cap can never weaken the block decision.
DEFAULT_ADVISORY_CAP = 10

# Model-by-window escalation ladder (child ca03 size handling). Estimated tokens →
# the smallest model whose window fits; escalate up on a context-limit signal.
MODEL_LADDER = (
    ("claude-haiku-4-5", 200_000),
    ("claude-sonnet-4-6", 1_000_000),
    ("claude-opus-4-8", 1_000_000),
)

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
    except Exception:
        listed = []
    for c in listed:
        cid = c.get("ticket_id")
        try:  # fetch full child state (deps + file_impact) for P5/P8
            children.append(rebar.show_ticket(cid, repo_root=repo_root))
        except Exception:
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
    )


def largest_window_tokens(model: str | None) -> int:
    """The largest context window the gate can escalate to for P8's budget. If the
    configured model is on the ladder, the ladder's top window applies (we can
    escalate up to it); an unknown model uses its own ladder entry if present, else
    the ladder's maximum. A model SMALLER than the ladder top (e.g. a haiku-only
    deployment) caps P8 at that model's window so P8 doesn't under-block."""
    if model:
        for name, _window in MODEL_LADDER:
            if name in model:
                # The gate escalates UP the ladder from this model, so the effective
                # ceiling is the max window at-or-above it.
                idx = [n for n, _ in MODEL_LADDER].index(name)
                return max(w for _, w in MODEL_LADDER[idx:])
    return MODEL_LADDER[-1][1]


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


def _is_context_limit_error(exc: Exception) -> bool:
    """Heuristic: does ``exc`` look like a provider context-window/too-many-tokens
    error (vs an unrelated failure)? Matches the common phrasings across providers."""
    msg = str(exc).lower()
    return any(
        s in msg
        for s in (
            "context",
            "too many tokens",
            "maximum context",
            "context_length",
            "prompt is too long",
            "input length",
            "exceeds the maximum",
            "token limit",
        )
    )


def _models_at_or_above(model: str | None) -> list[str]:
    """The model ladder from ``model`` upward (by window), for runtime escalation.
    Unknown/absent model → the whole ladder."""
    names = [n for n, _w in MODEL_LADDER]
    if model:
        for i, n in enumerate(names):
            if n in model:
                return names[i:]
    return list(names)


def _pass1_with_ladder(
    runner: Runner,
    cfg: LLMConfig,
    plan: str,
    chunk: list[dict],
    agentic: bool,
    events: list[str],
) -> list[dict[str, Any]]:
    """Run a Pass-1 finder call with the SIZE-HANDLING LADDER (ca03 AC4/AC6):

    1. run the criteria BATCH (chunk) at the configured model;
    2. on a context-limit signal, fall back to ONE CRITERION PER CALL (full content,
       minimal rubric — content is never chunked);
    3. on a context-limit signal for a single criterion, ESCALATE up the model ladder
       to a higher-context window and retry;
    4. if a single criterion still won't fit at the largest window, emit a FAILURE
       FINDING (P8: the ticket is too big to review in full — reduce/decompose it).

    Non-context errors drop the unit's findings (never abort the review), as before.
    ``events`` accumulates a human-readable ladder trace for the coverage record."""
    from dataclasses import replace

    try:
        return passes.pass1_chunk(runner, cfg, plan=plan, chunk=chunk, agentic=agentic)
    except Exception as exc:  # noqa: BLE001
        if not _is_context_limit_error(exc):
            return []  # unrelated failure → drop this unit's findings (never abort)

    # Step 2/3: drop to one-criterion-per-call, escalating the model per criterion.
    if len(chunk) > 1:
        events.append(f"batch of {len(chunk)} hit the context limit → one-criterion-per-call")
    out: list[dict[str, Any]] = []
    for crit in chunk:
        produced = False
        for model in _models_at_or_above(cfg.model):
            try:
                out.extend(
                    passes.pass1_chunk(
                        runner, replace(cfg, model=model), plan=plan, chunk=[crit], agentic=agentic
                    )
                )
                if model != cfg.model:
                    events.append(f"{crit['id']}: escalated to {model}")
                produced = True
                break
            except Exception as exc:  # noqa: BLE001
                if not _is_context_limit_error(exc):
                    produced = True  # non-size failure → drop, don't escalate
                    break
                continue  # context limit at this model → escalate to the next
        if not produced:
            # Step 4: still too big at the largest window, one criterion at a time.
            events.append(f"{crit['id']}: too big even at the largest model → failure finding")
            out.append(
                {
                    "finding": (
                        "The ticket is too large to review in full even for a single criterion "
                        f"({crit['id']}) at the largest context window."
                    ),
                    "criteria": [crit["id"]],
                    "location": "(whole plan)",
                    "evidence": [
                        "content exceeds the largest model window one-criterion-at-a-time"
                    ],
                    "scenarios": [],
                    "impact": "The plan cannot be reviewed whole; reduce/decompose it (P8/G5).",
                    "checklist_item": "- [ ] Reduce/decompose the ticket so it fits a review pass.",
                    "suggested_fix": "Split the ticket into smaller children.",
                    "tier": "DET",
                    "_too_big": True,
                }
            )
    return out


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
            except Exception:  # a failed pairing drops its findings, never aborts
                pass
            pairings += 1
    coverage["container"] = {
        "criteria": [c["id"] for c in container],
        "children": len(ctx.children),
        "pairings_evaluated": pairings,
    }
    return out


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
            except Exception:
                continue
            if log.get("ticket_type") == "session_log":
                bodies.append(f"# {log.get('title', '')}\n{log.get('description', '')}")
    except Exception:
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
    except Exception:
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
        except Exception as exc:  # LLM unavailable → DET-only review (advisory still works)
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
        except Exception as exc:
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

    findings: list[dict[str, Any]] = []
    ladder_events: list[str] = []
    max_workers = max(1, min(6, len(chunks) + len(agent)))
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        st_futs = [
            ex.submit(_pass1_with_ladder, runner, cfg, plan, ch, False, ladder_events)
            for ch in chunks
        ]
        ag_futs = [
            ex.submit(_pass1_with_ladder, runner, cfg, plan, [c], True, ladder_events)
            for c in agent
        ]
        for fu in st_futs + ag_futs:
            try:
                findings.extend(fu.result() or [])
            except Exception:  # a failed chunk drops its findings, never aborts the review
                pass
    if ladder_events:
        coverage["size_ladder"] = ladder_events

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
                    runner, cfg, plan=plan, session_log_text=log_text, summarized=summarized
                )
            )
        except Exception as exc:
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
    findings = [f for f in findings if not f.get("_too_big")]

    # Pass 2: one aggregate verification pass (agentic if any code-grounded finding).
    grounded = any(
        any(c in registry.CODEBASE_GROUNDED for c in f.get("criteria", [])) for f in findings
    )
    v_cfg = passes.verifier_cfg(cfg)
    verifs = passes.pass2_verify(runner, v_cfg, plan=plan, findings=findings, agentic=grounded)

    # Pass 3: deterministic decision + thresholds per criterion.
    crit_by_id = registry.by_id()
    decided: list[dict[str, Any]] = list(too_big)
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
