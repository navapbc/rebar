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
from typing import Any

from rebar.llm import review_kernel
from rebar.llm.config import LLMConfig
from rebar.llm.runner import Runner, get_runner

from . import registry
from .det_floor import PlanContext

# The Pass-1 finder machinery lives in :mod:`.pass1` (module-size seam, epic B /
# story B1). Re-exported here for the historical ``orchestrator.<name>`` call sites
# (attest.py + the test suite). The Pass-1 finder itself now runs only via the workflow's
# ProductionBatchRunner (which imports run_pass1 from .pass1 directly) — the bespoke
# _run_passes that invoked it here was retired in epic solid-timer-unison (WS1).
from .pass1 import (  # noqa: F401
    CONTAINER_CRITERIA,
    _run_container,
    _ticket_graph_blob,
    material_fingerprint,
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
        if cid is None:
            children.append(c)
            continue
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
        # fires only when a session log is linked — handled separately by the Pass-1
        # finder (run_pass1), so it never enters the normal single/agent routing.
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


# ── verdict assembly (the SINGLE source of truth, shared by the v3 plan-review ────
#    workflow's `uses` ops — story B2; NO duplicated assembly logic) ──────────────
def partition_findings(
    det_blocks: list[dict[str, Any]],
    det_advisories: list[dict[str, Any]],
    llm_findings: list[dict[str, Any]],
    *,
    advisory_cap: int = DEFAULT_ADVISORY_CAP,
) -> dict[str, list[dict[str, Any]]]:
    """Merge DET-floor + decided-LLM findings into the verdict partition and apply the
    advisory cap. Mints each finding's stable ``id``. Returns
    ``{blocking, surfaced, overflow, indeterminate, dropped}`` — blocking findings are
    EXEMPT from the cap (all returned); advisory is sorted by priority then split at the
    cap. The deterministic core the workflow's ``plan_review_decide`` op calls (no
    duplicated partition logic)."""
    blocking: list[dict[str, Any]] = []
    advisory: list[dict[str, Any]] = []
    dropped: list[dict[str, Any]] = []
    indeterminate: list[dict[str, Any]] = []

    for f in det_blocks:
        blocking.append(
            {
                **f,
                "id": mint_finding_id(f),
                "decision": "block",
                "severity": "critical",
                "priority": 1.0,
                "validity": 1.0,
                "impact": 1.0,
                "tier": "DET",
            }
        )
    for f in det_advisories:
        advisory.append(
            {
                **f,
                "id": mint_finding_id(f),
                "decision": "advisory",
                "severity": "minor",
                "priority": 0.4,
                "validity": 1.0,
                "impact": 0.4,
                "tier": "DET",
            }
        )
    for f in llm_findings:
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

    # Guard the load-bearing invariant: the cap must NEVER see a blocking finding
    # (all blocking findings are returned regardless of N). A refactor that leaked
    # one into `advisory` would otherwise silently drop a block — fail loud instead.
    assert not any(f.get("decision") == "block" for f in advisory), (
        "blocking finding leaked into the advisory cap"
    )
    advisory.sort(key=lambda f: -float(f.get("priority", 0.0)))
    return {
        "blocking": blocking,
        "surfaced": advisory[:advisory_cap],
        "overflow": advisory[advisory_cap:],
        "indeterminate": indeterminate,
        "dropped": dropped,
    }


def finalize_verdict(
    ctx: PlanContext,
    parts: dict[str, list[dict[str, Any]]],
    *,
    coaching: list[dict[str, Any]],
    coverage: dict[str, Any],
    runner_name: str | None,
    model: str | None,
) -> dict[str, Any]:
    """Assemble the terminal ``plan_review_verdict`` from a partition + coaching +
    coverage. Computes the verdict string (a DET block ⇒ BLOCK; an unavailable/
    unresolved LLM tier ⇒ INDETERMINATE; else PASS), records the counts on coverage,
    and returns the verdict dict. Shared by ``drift_refresh`` and the workflow ops."""
    blocking = parts["blocking"]
    surfaced = parts["surfaced"]
    overflow = parts["overflow"]
    indeterminate = parts["indeterminate"]
    dropped = parts["dropped"]
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
        "runner": runner_name,
        "model": model,
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
        # The probe runs the SOLE plan-review gate workflow restricted to the cheap
        # E4+G1G2 criteria (PROBE MODE), not a bespoke pass pipeline (epic solid-timer-unison
        # WS1: the bespoke _run_passes/pass2_verify path was retired). The verify cfg is tuned
        # to the non-frontier verifier model the same way review_plan does (_verifier_cfg) so
        # the probe's verify is parity-faithful with the prior bespoke verifier_cfg.
        from rebar.llm.plan_review import _verifier_cfg
        from rebar.llm.workflow import gate_dispatch

        probe_verdict = gate_dispatch.produce_plan_review_verdict(
            ctx,
            _verifier_cfg(cfg),
            runner=runner_sel,
            advisory_cap=DEFAULT_ADVISORY_CAP,
            repo_root=repo_root,
            probe_criteria=list(_PROBE_CRITERIA),
        )
    except Exception:  # noqa: BLE001 — probe unavailable → FULL re-review (fail-safe)
        logger.warning("drift-probe failed; falling back to a full re-review", exc_info=True)
        return None

    # A non-PASS probe (a BLOCK, or an INDETERMINATE from a degraded/unavailable LLM tier)
    # is inconclusive → escalate to a FULL re-review; never refresh on a hollow probe.
    if probe_verdict.get("verdict") != "PASS" or not probe_verdict.get("coverage", {}).get(
        "llm_ran"
    ):
        return None
    # Escalate iff any surfaced finding cites a drifted file (a blocking finding already made
    # the verdict non-PASS above). Mirrors the prior bespoke decision/citations check, now over
    # the verdict's surfaced partitions (blocking + advisory + overflow).
    surfaced = [
        *(probe_verdict.get("blocking") or []),
        *(probe_verdict.get("advisory") or []),
        *(probe_verdict.get("overflow") or []),
    ]
    for f in surfaced:
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


def pass3_over_findings(
    findings: list[dict[str, Any]], verifs: dict[int, dict[str, Any]]
) -> list[dict[str, Any]]:
    """Deterministic Pass-3 over the verifiable findings for the PLAN-REVIEW gate: this
    is the thin consumer wrapper that resolves plan-review's per-criterion thresholds +
    posture from its criteria registry, then delegates the math to the shared review
    kernel (:func:`rebar.llm.review_kernel.pass3_over_findings`). The decision core +
    the per-finding loop live in the kernel — only the registry-driven threshold LOOKUP
    is plan-review-specific (epic vivid-gang-day WS1). The too_big/shed routing is the
    caller's (it differs by index-domain)."""
    crit_by_id = registry.by_id()

    def _threshold_for(criteria: Any) -> tuple[float, bool]:
        default_bt = review_kernel.DEFAULT_BLOCK_THRESHOLD
        thresholds = [
            float(crit_by_id.get(c, {}).get("block_threshold", default_bt)) for c in criteria
        ]
        blocking_enabled = any(
            str(crit_by_id.get(c, {}).get("default_posture", "advisory")).lower() == "blocking"
            for c in criteria
        )
        bt = min(thresholds) if thresholds else default_bt
        return bt, blocking_enabled

    return review_kernel.pass3_over_findings(findings, verifs, threshold_for=_threshold_for)


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
