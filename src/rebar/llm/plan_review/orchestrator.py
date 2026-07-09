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

import contextlib
import contextvars
import hashlib
import logging
from collections.abc import Iterator
from typing import Any

from rebar.llm import criteria as _criteria
from rebar.llm import review_kernel
from rebar.llm.config import (
    LLMConfig,
    current_code_root,
    current_tickets_root,
    resolve_code_root,
)
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
# findings are EXEMPT — the cap can never weaken the block decision. Raised 10→20
# so a thorough plan's long tail is surfaced rather than silently truncated; the
# overflow COUNT is still reported (coverage.counts.advisory_overflow) and the full
# overflow set persists to the REVIEW_RESULT sidecar.
DEFAULT_ADVISORY_CAP = 20

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
# Within ONE plan-review gate run the four-pass workflow assembles the same ticket
# graph ~4× (precheck + assemble_criteria + verify_inputs + coach_inputs each call
# assemble_context), an N+1 store read each time (show_ticket + list_tickets + a
# show_ticket per child). A run-scoped memo collapses those repeated identical calls
# to ONE read of the graph: `assemble_context_cache()` activates a per-run cache (a
# ContextVar — thread/asyncio-task-safe, never leaking across runs), and inside it
# `assemble_context` returns the SAME PlanContext object for the same key. OUTSIDE a
# scope the cache is absent and every call reads fresh (byte-identical to the prior
# behavior — no caller has to opt in). The key spans every input that changes the
# result: the ticket id, the explicit `repo_root`, the cfg fields that flow into the
# context (`repo_path` → the resolved code root, `model` → largest_window_tokens),
# and the active gate read-roots (code + tickets ContextVars) so a snapshot change is
# never served a stale entry.
_assemble_cache: contextvars.ContextVar[dict[Any, PlanContext] | None] = contextvars.ContextVar(
    "rebar_plan_review_assemble_cache", default=None
)


@contextlib.contextmanager
def assemble_context_cache() -> Iterator[None]:
    """Activate a run-scoped :func:`assemble_context` memo for the dynamic extent of the
    ``with`` block (one plan-review gate run). Repeated ``assemble_context`` calls with the
    same key inside the block return the SAME cached :class:`PlanContext` instead of
    re-reading the ticket graph; the cache is dropped on exit, so it never leaks across runs
    or tickets. Nesting reuses the already-active cache (idempotent)."""
    if _assemble_cache.get() is not None:
        # Already inside an active scope (nested) — reuse it; the outer scope owns reset.
        yield
        return
    token = _assemble_cache.set({})
    try:
        yield
    finally:
        _assemble_cache.reset(token)


def _assemble_cache_key(ticket_id: str, repo_root, cfg: LLMConfig | None) -> tuple:
    """The memo key: every input that can change ``assemble_context``'s result. Includes the
    active gate read-roots so a snapshot change within a process is never served a stale entry
    (the resolved code root + the store the reads run against both feed the returned context)."""
    return (
        ticket_id,
        str(repo_root) if repo_root is not None else None,
        cfg.repo_path if cfg else None,
        cfg.model if cfg else None,
        current_code_root(),
        current_tickets_root(),
    )


def assemble_context(
    ticket_id: str, *, repo_root=None, cfg: LLMConfig | None = None
) -> PlanContext:
    """Build the whole-ticket :class:`PlanContext` from rebar reads (ticket + its
    direct children, each whole). The largest context window is taken from the
    model ladder for P8's budget.

    Inside an active :func:`assemble_context_cache` scope (one gate run) the result is
    memoized by :func:`_assemble_cache_key`, so the workflow's repeated calls hit the
    cache and the ticket graph is read ONCE. Outside a scope this reads fresh every time
    (the historical behavior — the returned context is byte-identical either way)."""
    cache = _assemble_cache.get()
    if cache is not None:
        key = _assemble_cache_key(ticket_id, repo_root, cfg)
        hit = cache.get(key)
        if hit is not None:
            return hit
        ctx = _assemble_context_uncached(ticket_id, repo_root=repo_root, cfg=cfg)
        cache[key] = ctx
        return ctx
    return _assemble_context_uncached(ticket_id, repo_root=repo_root, cfg=cfg)


def _assemble_context_uncached(
    ticket_id: str, *, repo_root=None, cfg: LLMConfig | None = None
) -> PlanContext:
    """The actual N+1 store read (ticket + direct children, each whole). Always reads — the
    run-scoped memo lives in :func:`assemble_context`, which delegates here on a cache miss."""
    from rebar import _reads

    state = _reads.show_ticket(ticket_id, repo_root=repo_root)
    canonical = state.get("ticket_id", ticket_id)
    children: list[dict[str, Any]] = []
    try:
        listed = _reads.list_tickets(parent=canonical, repo_root=repo_root) or []
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
            children.append(_reads.show_ticket(cid, repo_root=repo_root))
        except Exception:  # noqa: BLE001 — per-child best-effort full-state fetch; fall back to the summary
            children.append(c)
    return PlanContext(
        ticket_id=canonical,
        ticket_type=state.get("ticket_type", ""),
        title=state.get("title", ""),
        description=state.get("description", ""),
        state=state,
        children=children,
        repo_root=resolve_code_root(
            repo_root,
            cfg_repo_path=cfg.repo_path if cfg else None,
            # Snapshot-or-None: inside a gate this picks up the active attested snapshot
            # (fixing the det-floor P2 `no_repo_root` abstain); outside a gate it stays
            # None — this lightweight builder must not force a checkout root, which would
            # induce checkpoint/cache writes into the live checkout.
            allow_checkout_fallback=False,
        ),
        # The pinned TICKET-STORE snapshot root (attested), captured HERE on the
        # assembling thread where the ContextVar is set — the pass-1 fan-out runs in
        # worker threads that would NOT inherit it. ``None`` (local / no gate) → the live
        # checkout store. Downstream ticket reads use this, never ``repo_root`` (code).
        tickets_root=current_tickets_root(),
        largest_window_tokens=largest_window_tokens(cfg.model if cfg else None),
        centrality=_centrality(state, children),
    )


# ── delivered-children manifest (completion-aware container plan-review, epic 66ac / 94fd) ──────
def _extract_ac_section(description: str) -> str:
    """Extract the ``## Acceptance Criteria`` section BODY (verbatim, up to the next ``## ``
    heading) from a ticket description. Empty when there is no AC section. Mirrors the heading-scan
    ``det_floor._ac_item_lines`` uses, but keeps the WHOLE section (not just the ``- [ ]`` lines)
    so the completion sub-call sees the child's full acceptance text."""
    out: list[str] = []
    found = False
    for ln in description.split("\n"):
        if not found and ln.strip().lower().startswith("## acceptance criteria"):
            found = True
            continue
        if found and ln.startswith("## "):
            break
        if found:
            out.append(ln)
    return "\n".join(out).strip()


def delivered_children_manifest(container_id: str, *, repo_root=None) -> list[dict[str, Any]]:
    """The DELIVERED-children manifest the Pass-2 completion sub-call
    (:func:`rebar.llm.plan_review.passes.pass2_completion`) receives.

    Enumerates ``container_id``'s children (the SAME ``list_tickets(parent=…)`` +
    per-child full ``show_ticket`` read as :func:`_assemble_context_uncached`, so delivery + the
    supersede branch see full child state), then keeps only the DELIVERED ones per
    :func:`rebar.llm.plan_review.attest.delivered_now` — returning each as
    ``{"ticket_id", "ac_text"}`` (its ``## Acceptance Criteria`` section). Fail-safe: an
    enumeration error yields an EMPTY manifest, and ``pass2_completion`` then classifies nothing so
    the floor drops nothing."""
    from rebar import _reads

    from . import attest

    try:
        listed = _reads.list_tickets(parent=container_id, repo_root=repo_root) or []
    except Exception:  # noqa: BLE001 — fail-safe: no manifest (the floor drops nothing), logged
        logger.warning(
            "could not list children of %s for the delivered manifest", container_id, exc_info=True
        )
        return []
    children: list[dict[str, Any]] = []
    for c in listed:
        cid = c.get("ticket_id")
        if cid is None:
            children.append(c)
            continue
        try:  # full child state (status + attestation + supersede deps) for delivered_now
            children.append(_reads.show_ticket(cid, repo_root=repo_root))
        except Exception:  # noqa: BLE001 — per-child best-effort full-state fetch; fall back to summary
            children.append(c)
    manifest: list[dict[str, Any]] = []
    for child in children:
        if attest.delivered_now(child, children, repo_root=repo_root):
            manifest.append(
                {
                    "ticket_id": child.get("ticket_id"),
                    "ac_text": _extract_ac_section(child.get("description", "") or ""),
                }
            )
    return manifest


# ── verification contract-violation collection (run-scoped) ──────────────────────
# Pass-2's verifier→Pass-3 reshape (the `plan_review_decide` op) detects contract violations
# (malformed / duplicate / out-of-range verification indices) via the shared
# `review_kernel.reshape_verifications` seam. Under the expand-contract posture (epic
# drag-gripe-brake) those NEVER change the verdict — they are surfaced as ADDITIVE observability:
# an ERROR log + a `verification_contract_violations` entry on the verdict coverage, present ONLY
# when non-empty (so a clean run's verdict stays byte-identical → attestation-safe). `decide` and
# `coach` run as SEPARATE workflow steps, so the report is carried between them by a run-scoped
# ContextVar (the same mechanism as the assemble memo above), activated once per gate run by
# `produce_plan_review_verdict`.
_contract_violations: contextvars.ContextVar[list[dict[str, Any]] | None] = contextvars.ContextVar(
    "rebar_plan_review_contract_violations", default=None
)


@contextlib.contextmanager
def collect_contract_violations() -> Iterator[None]:
    """Activate a run-scoped sink for verification contract violations for the dynamic extent of
    one plan-review gate run. Nesting reuses the active sink (idempotent); the sink is dropped on
    exit so it never leaks across runs/tickets."""
    if _contract_violations.get() is not None:
        yield
        return
    token = _contract_violations.set([])
    try:
        yield
    finally:
        _contract_violations.reset(token)


def record_contract_violation(summary: dict[str, Any]) -> None:
    """Record one NON-EMPTY contract-violation summary if a sink is active; a no-op outside a
    :func:`collect_contract_violations` scope (so unit-testing ``plan_review_decide`` in isolation
    never raises and never leaks)."""
    sink = _contract_violations.get()
    if sink is not None and summary:
        sink.append(dict(summary))


def drain_contract_violations() -> list[dict[str, Any]]:
    """Return + clear the violations recorded in the active sink (empty list when none recorded,
    or when no sink is active)."""
    sink = _contract_violations.get()
    if not sink:
        return []
    drained = list(sink)
    sink.clear()
    return drained


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
    # Load the EFFECTIVE criteria (built-ins ∪ activated project criteria from the
    # `.rebar/criteria_routing.json` overlay). repo_root may be None here (the lightweight
    # context builder); registry resolves it to config.repo_root() so the overlay is honored.
    for c in registry.load_criteria(repo_root=ctx.repo_root):
        cid = c["id"]
        # ISF is special: it is fed the linked SESSION LOG (not the rubric chunk) and
        # fires only when a session log is linked — handled separately by the Pass-1
        # finder (run_pass1), so it never enters the normal single/agent routing.
        if cid == "ISF":
            continue
        # exec:DET criteria (project invariants + the packaged floor) run in the DETERMINISTIC
        # phase (det_floor), NEVER as an LLM finder — they carry no prompt and must never reach
        # pass1_chunk. Read `exec` DIRECTLY here (not via exec_tier, which has no DET arm and would
        # misroute a DET criterion to 1-TURN). Story 7f0d.
        if str(c.get("exec", "")).upper() == "DET":
            continue
        if not registry.applies(
            c,
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


def _any_blocking_criterion(findings: list[dict[str, Any]]) -> bool:
    """True if any finding maps to a criterion whose registry posture is ``blocking`` —
    i.e. a finding that COULD block were it verified. Used by ``finalize_verdict`` to decide
    a verify-failure verdict (bug 59bc): without Pass-2 there is no priority, so a finding on
    a blocking-enabled criterion is treated as potentially-blocking (fail-closed); a review
    whose preserved findings are all on advisory-only criteria can never block → fail-open."""
    blocking_ids = {
        cid
        for cid, c in registry.by_id().items()
        if str(c.get("default_posture", "advisory")).lower() == "blocking"
    }
    return any(set(f.get("criteria", []) or []) & blocking_ids for f in findings)


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
    elif coverage.get("llm_unavailable"):
        verdict = "INDETERMINATE"
    elif coverage.get("verify_failed"):
        # Pass-2 verify could not run (e.g. the agentic verifier exhausted its step budget),
        # so the Pass-1 findings are PRESERVED but unverified → all INDETERMINATE (bug 59bc).
        # Fail the claim ONLY IF a preserved finding sits on a blocking-enabled criterion: with
        # no Pass-2 we cannot compute priority, so such a finding is "potentially blocking" and
        # we cannot rule out a real block → INDETERMINATE (fail-closed). When no preserved
        # finding could ever block, the review fails OPEN → PASS (matching the gate's stated
        # fail-open posture; the un-verified findings are still surfaced for coaching).
        verdict = "INDETERMINATE" if _any_blocking_criterion(indeterminate) else "PASS"
    elif indeterminate and not surfaced:
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
    # Deep-link each coaching note to its criterion's authoring-guide section (WS10). Anchor on
    # the FIRST criterion of the finding(s) the note addresses; fall back to the guide base URL.
    # Additive `guide_url` field — passthrough-safe for every coaching consumer (they read the
    # `coaching` prose). Plan-review-specific (NOT the shared render_coach_notes). See ADR/docs.
    from rebar import config as _config

    _base = _config.plan_review_docs_url(ctx.repo_root)
    _crit_by_fid = {
        f.get("id"): (f.get("criteria") or [])
        for group in (blocking, surfaced, overflow, indeterminate, dropped)
        for f in group
    }
    for note in coaching:
        crit = next(
            (c for fid in (note.get("finding_refs") or []) for c in _crit_by_fid.get(fid, [])),
            None,
        )
        note["guide_url"] = f"{_base}#{crit.lower()}" if crit else _base
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

    if ctx.ticket_type in ("bug", "session_log", "code_review"):
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

    # DELEGATE the (block_threshold, blocking) resolution to the SHARED criteria layer with
    # gate="plan_review" (story 5065): plan-review derives blocking from `default_posture ==
    # "blocking"`. The descriptor map (`by_id()`) carries block_threshold + default_posture, so
    # the resolution is byte-identical to the pre-unification private resolver.
    def _threshold_for(criteria: Any) -> tuple[float, bool]:
        return _criteria.threshold_for(criteria, crit_by_id, gate="plan_review")

    # Plan-review dispatches its own impact model (story fishable-apivorous-redhead):
    # severity-first MAX + hard override + detection amplifier over the 7 plan-severity axes,
    # INSTEAD of the mean `impact`. The signed-verdict shape is unchanged; code-review keeps
    # the kernel default (no impact_fn) and is byte-unchanged.
    return review_kernel.pass3_over_findings(
        findings, verifs, threshold_for=_threshold_for, impact_fn=review_kernel.impact_plan
    )


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
