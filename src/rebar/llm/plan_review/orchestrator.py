"""The plan-review orchestrator (child ca03) — the engine that runs the gate.

It owns the generic flow, reusing rebar.llm extension points (the runner, the
prompt/contract model) and the sibling modules (:mod:`.det_floor`, :mod:`.passes`,
:mod:`.registry`):

1. **Assemble** the whole ticket context (plan + children) from rebar's own reads
   — content is ALWAYS whole; never truncated, never content-chunked.
2. **DET tier** — run the deterministic floor (P1–P9) via the code executor; its
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
import logging
import time
from typing import Any

from rebar.llm.config import LLMConfig
from rebar.llm.errors import LLMUnavailableError
from rebar.llm.runner import Runner, get_runner

from . import det_floor, passes, registry
from .det_floor import PlanContext

# The Pass-1 finder machinery lives in :mod:`.pass1` (module-size seam, epic B /
# story B1). Re-exported here for the historical ``orchestrator.<name>`` call sites
# (attest.py + the test suite); ``run_pass1`` is invoked by ``_run_passes`` below.
from .pass1 import (  # noqa: F401
    CONTAINER_CRITERIA,
    _run_container,
    _ticket_graph_blob,
    material_fingerprint,
    run_pass1,
)

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


# The Pass-4 move registry + loader live with their consumer (pass4_coach) in
# :mod:`.passes`; re-exported here for the historical ``orchestrator.MOVE_REGISTRY`` /
# ``orchestrator.load_move_registry`` call sites (module-size seam, child 75a9).
from .passes import MOVE_REGISTRY, load_move_registry  # noqa: E402,F401


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
        except LLMUnavailableError as exc:
            # SYSTEMIC failure — the LLM tier could not run at all (missing agents extra,
            # missing/invalid key, provider auth/connection/rate-limit). This must NOT
            # degrade to a hollow DET-only PASS (fuel-posse-ball): mark the tier
            # unavailable so the verdict becomes INDETERMINATE and is never signed.
            logger.warning("plan-review LLM tier unavailable: %s", exc)
            coverage["llm_error"] = str(exc)
            coverage["llm_ran"] = False
            coverage["llm_unavailable"] = True
        except Exception as exc:  # noqa: BLE001 — an UNEXPECTED tier failure: also never a silent
            # pass. Treat as unavailable (INDETERMINATE, unsigned) + record in-band/logger.
            logger.warning("plan-review LLM tier failed unexpectedly", exc_info=True)
            coverage["llm_error"] = str(exc)
            coverage["llm_ran"] = False
            coverage["llm_unavailable"] = True
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

    # A DET block is still an actionable BLOCK even with no LLM. Otherwise, a SYSTEMIC
    # LLM-tier failure (llm_unavailable) makes the review INDETERMINATE — never a hollow
    # PASS — so it is not signed (fuel-posse-ball). A genuine unsupported-stack abstain
    # (the tier ran, a criterion couldn't ground) is NOT llm_unavailable → still PASS.
    if blocking:
        verdict = "BLOCK"
    elif coverage.get("llm_unavailable") or (indeterminate and not surfaced):
        verdict = "INDETERMINATE"
    else:
        verdict = "PASS"
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


# ── progressive drift-refresh (Story 2, epic boil-golem-veto / ADR 0002) ──────────
_PROBE_CRITERIA = ("E4", "G1G2")  # "is the plan accurate vs the codebase" — both mandatory in v1


def drift_refresh(
    ctx: PlanContext, cfg: LLMConfig, *, runner: Runner | None = None, repo_root=None
) -> dict[str, Any] | None:
    """The progressive (tripwire) drift re-review. If the ticket's attestation is stale
    ONLY because reviewed code drifted (ticket material + criteria-registry unchanged),
    run a cheap probe (E4+G1G2) against the CURRENT code; if the plan still holds, REFRESH
    the attestation (re-sign the prior verdict with current dependency hashes) and return a
    refreshed PASS verdict. Returns None when not applicable, or when the probe escalates
    (a blocking finding, or a finding citing a drifted dependency file) — the caller then
    runs the FULL review. Soundness is whole-verdict, gated by the probe: NO per-criterion
    finding reuse, so the (unenforced) code-blind criterion partition is not relied upon."""
    from . import attest

    if ctx.ticket_type in ("bug", "session_log"):
        return None
    cand = attest.drift_refresh_candidate(ctx.ticket_id, repo_root=repo_root)
    if cand is None:
        return None
    current = attest._rehash(cand["deps"].keys(), repo_root=repo_root)
    drifted = {p for p, h in cand["deps"].items() if current.get(p) != h}

    runner_sel = runner or get_runner(cfg)
    try:
        runner_sel.preflight()
        probe_crits = [c for c in (registry.by_id().get(cid) for cid in _PROBE_CRITERIA) if c]
        findings = _run_passes(ctx, cfg, runner_sel, [], probe_crits, {})
    except Exception:  # noqa: BLE001 — probe unavailable → FULL re-review (fail-safe)
        logger.warning("drift-probe failed; falling back to a full re-review", exc_info=True)
        return None

    # Escalate iff any blocking finding, or any block/advisory finding citing a drifted file.
    for f in findings:
        if f.get("decision") == "block":
            return None
        if f.get("decision") in ("block", "advisory"):
            cited = {c.get("path") for c in (f.get("citations") or []) if isinstance(c, dict)}
            if cited & drifted:
                return None

    # Clean probe → the drift was immaterial → refresh the attestation wholesale.
    sig = attest.refresh_attestation(
        ctx.ticket_id, cand["manifest"], probe="PASS", repo_root=repo_root
    )
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
        "coverage": {
            "drift_refresh": True,
            "probe": "PASS",
            "probe_criteria": list(_PROBE_CRITERIA),
            "drifted_files": sorted(drifted),
            "llm_ran": True,
        },
        "runner": runner_sel.name,
        "model": cfg.model,
        "material_fingerprint": attest.manifest_material(cand["manifest"]),
        "sidecar_emitted": False,
        "signature": {
            "signed": True,
            "key_id": sig.get("key_id"),
            "head_sha": sig.get("head_sha"),
            "refreshed": True,
        },
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
    findings = run_pass1(ctx, cfg, runner, single, agent, coverage)

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
