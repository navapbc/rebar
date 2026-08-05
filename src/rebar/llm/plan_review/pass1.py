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

import contextvars
import hashlib
import json
import logging
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from rebar._plan_clarity import evaluate_plan_clarity
from rebar.llm import capabilities
from rebar.llm.config import LLMConfig
from rebar.llm.errors import LLMUnavailableError
from rebar.llm.runner import Runner

from . import det_floor, generation, passes, registry, sidecar, sizing
from .det_floor import PlanContext

logger = logging.getLogger(__name__)

# Private aliases for the sizing helpers the moved code calls (preserve the
# historical call sites verbatim).
_pass1_with_ladder = sizing.pass1_with_ladder
_shed_to_budget = sizing.shed_to_budget


# Container criteria (parent + one child at a time); handled by the dedicated
# per-child loop, never the normal agent path.
CONTAINER_CRITERIA = ("G3", "G4", "decomp-shape")

# The minimum prompt-prefix the anthropic cache will write/read. Below this the parent-plan
# system prefix never caches, so WARMING would just add a serialized call for no read benefit —
# fan out directly instead (story ba7e). Re-exported from ``llm.capabilities`` (bug 7a79) so the
# cache-effectiveness warning in ``llm/structured_run.py`` shares ONE definition of the floor
# with this warm-up decision instead of hand-syncing a second literal; the historical call sites
# below read this name unchanged.
CACHE_MIN_PREFIX_TOKENS = capabilities.CACHE_MIN_PREFIX_TOKENS


def _prefix_tokens(plan: str) -> int:
    """Tokens of the ACTUAL Pass-1 cached prefix — ``shared_plan_prefix(plan)`` (the
    stance preamble + the whole plan), the bytes the warm gate must measure (story 9374).
    Measuring the plan alone would under-count by the constant-size preamble and skip
    warming a prefix that would in fact have cached."""
    from rebar.llm.prompting import prompts

    return det_floor.est_tokens(prompts.shared_plan_prefix(plan))


# Concurrency cap for the container fan-out pool (a NEW pool — the Pass-1 pool is closed
# by the time the container criteria run).
_CONTAINER_MAX_WORKERS = 6


def _submit_ctx(ex: ThreadPoolExecutor, fn, *args):
    """Submit ``fn(*args)`` to the pool carrying a COPY of the current context, so the
    gate-session ContextVars (`_in_gate_session` / `_active_code_root`) — which raw worker
    threads do NOT inherit — reach the worker. Without this, an agentic ``runner.run`` in a
    worker reads `in_gate_session()` == False and `assert_gated` raises before any LLM call.
    A FRESH copy per task: a Context cannot be entered concurrently (copy_context().run on the
    same Context from two threads raises). Evaluated here (the submitting thread) so it captures
    the active session."""
    return ex.submit(contextvars.copy_context().run, fn, *args)


def _too_big_finding(
    criteria: list[dict], child: dict, pair_tokens: int, budget: int
) -> dict[str, Any]:
    """The DET failure finding for a (parent + child) pairing that cannot fit the
    largest window together — 'reduce the ticket', not a silent skip. Emitted WITHOUT an
    LLM call, so it stays out of the fan-out. Tags ALL container criteria (the merged call
    that would have evaluated them can't run at this size)."""
    ids = [c["id"] for c in criteria]
    return {
        "finding": (
            f"The (parent + child {child.get('ticket_id')}) pairing is too big to "
            f"review together for {'/'.join(ids)} (~{pair_tokens} tokens > budget)."
        ),
        "criteria": list(ids),
        "location": f"child {child.get('ticket_id')}",
        "evidence": [f"parent+child ~{pair_tokens} tokens exceeds ~{budget}"],
        "scenarios": [],
        "impact": "Container coverage/consistency cannot be checked at this size.",
        "checklist_item": (
            f"- [ ] Reduce the parent or child {child.get('ticket_id')} so they review together."
        ),
        "suggested_fix": "Decompose the oversized ticket(s).",
        "tier": "DET",
        # COHORT (WS9): this container-failure finding bypasses pass1_container, but its cohort is
        # deterministically the merged container criteria — stamp it so it isn't excluded from
        # contamination analysis under the missing-cohort-as-unknown rule.
        "cohort": sorted(ids),
    }


def build_sibling_roster(children: list[dict[str, Any]]) -> str:
    """The COMPLETE sibling roster fed to the container pass (G3/G4) — the SINGLE source
    for every ``passes.pass1_container`` caller (this module, ``fidelity_spot_eval``, and
    ``evals.eval_solver``), so the production path and the eval harnesses cannot diverge.

    One block per child: its ``- <ticket_id>: <title>`` line, then that child's acceptance
    criteria indented two spaces beneath it. G3's rubric instructs the reviewer to discharge
    its "flag an absence only if NO sibling covers it" test against those items — a burden a
    title-only roster could never discharge, which is why G3 could not fire on an uncovered
    parent criterion (bug creamy-cocksure-elkhound). A child with no parseable
    ``## Acceptance Criteria`` keeps its bare line, so the roster degrades to the historical
    title-only shape rather than dropping the child."""
    lines: list[str] = []
    for child in children:
        lines.append(f"- {child.get('ticket_id')}: {child.get('title', '')}")
        for item in evaluate_plan_clarity(child.get("description") or "").ac_items:
            lines.append(f"  {item}")
    return "\n".join(lines)


def _timed_pairing(
    runner: Runner,
    cfg: LLMConfig,
    ctx: PlanContext,
    roster: str,
    criteria: list[dict],
    bin_children: list[dict],
) -> tuple[list[dict[str, Any]], dict[str, Any], Exception | None]:
    """Run ONE container pairing — the parent + a BIN of one-or-more whole children
    evaluated against ALL container criteria (G3+G4) in ONE merged+packed call (stories
    98c6 + 1762) — timed, NEVER raising; returns ``(findings, pairing_record, exc)``. A
    failed pairing yields empty findings + the exception (the caller decides: a SYSTEMIC
    failure on the warming call aborts; every other failure just drops that pairing's
    findings, matching the sequential baseline). Safe to run in the fan-out pool — each
    call builds its own agent + event loop (the Pass-1 pool already drives ``runner.run``
    concurrently across threads).

    The call's ``_usage`` dict is embedded as ``pairing_record["usage"]`` (``{}`` on a
    raising call — a raising attempt's usage is unrecoverable, see
    :func:`sizing.usage_record`), so the observability record and the usage accumulator
    read ONE field."""
    t0 = time.monotonic()
    exc: Exception | None = None
    findings: list[dict[str, Any]] = []
    usage: dict[str, Any] = {}
    try:
        findings, usage = passes.pass1_container(
            runner,
            cfg,
            parent_plan=ctx.plan_text,
            children=bin_children,
            criteria=criteria,
            sibling_roster=roster,
        )
    except Exception as e:  # noqa: BLE001 — capture; the caller classifies systemic vs not
        exc = e
    dt = time.monotonic() - t0
    record = {
        "criteria": [c["id"] for c in criteria],
        "children": [c.get("ticket_id") for c in bin_children],
        "seconds": round(dt, 1),
        "findings": len(findings),
        "error": type(exc).__name__ if exc else None,
        "usage": usage,
    }
    return findings, record, exc


def _run_container(
    ctx: PlanContext,
    cfg: LLMConfig,
    runner: Runner,
    container: list[dict],
    coverage: dict[str, Any],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Run the container criteria (G3/G4) as (parent + ONE child) pairings, both whole,
    CONCURRENTLY, and aggregate. Returns ``(findings, call_records)`` — one
    :func:`sizing.usage_record` per COMPLETED pairing (a failed pairing contributes
    nothing; the too-big failure finding makes no LLM call, so it contributes nothing).
    A pairing too big for the largest window is a failure
    finding (reduce the ticket), not a skip. The complete sibling roster is fed so
    absence findings can be cross-checked against ALL siblings before they stand.

    WARM-THEN-FAN-OUT (story ba7e): with S1's anthropic prompt caching, a NAIVE
    concurrent fan-out makes every pairing MISS+WRITE the (whole parent plan) cache
    prefix (~20× input cost, no read benefit). So when the parent prefix is large enough
    to cache, run ONE pairing to completion FIRST to warm the cache, then fan the rest
    out so they READ the warmed prefix. The aggregate finding set equals the sequential
    baseline — each in-budget pairing runs exactly once (no dup/drop)."""
    roster = build_sibling_roster(ctx.children)
    budget = sizing.container_budget(ctx.largest_window_tokens)
    # The roster rides the CACHED PREFIX (passes.pass1_container puts it in the system
    # prompt), so it is part of every pairing's prefix: count it here or pack_container_bins
    # under-estimates the prefix and can pack a bin over the window budget.
    parent_tokens = det_floor.est_tokens(ctx.plan_text) + det_floor.est_tokens(roster)
    out: list[dict[str, Any]] = []
    pairing_records: list[dict[str, Any]] = []
    call_records: list[dict[str, Any]] = []
    container_t0 = time.monotonic()

    # BIN-PACK the children into merged pairings (stories 98c6 merge + 1762 bin-pack): all
    # container criteria (G3+G4) run in ONE call per BIN, and small children pack together
    # up to the window budget (parent + all packed children, each WHOLE — never chunked).
    # A child whose parent+child ALONE exceeds budget is oversized → the single-child
    # too-big failure finding (NO LLM call, kept out of the fan-out).
    pairings, oversized = sizing.pack_container_bins(ctx.children, parent_tokens, budget)
    for child in oversized:
        pair_tokens = parent_tokens + det_floor.est_tokens(
            f"{child.get('title', '')}\n{child.get('description', '')}"
        )
        out.append(_too_big_finding(container, child, pair_tokens, budget))

    logger.info(
        "plan-review container fan-out: criteria %s over %d child(ren) packed into %d "
        "merged bin(s) (+%d oversized) = %d in-budget agentic pairing(s), parallel "
        "warm-then-fan-out (parent ~%d tokens)",
        [c["id"] for c in container],
        len(ctx.children),
        len(pairings),
        len(oversized),
        len(pairings),
        parent_tokens,
    )

    # WARM-THEN-FAN-OUT gate: only worth warming when the parent prefix actually caches
    # (>= the cache floor) AND there is more than one pairing to amortize it over.
    warm = parent_tokens >= CACHE_MIN_PREFIX_TOKENS and len(pairings) >= 2
    warmed = False
    to_pool = pairings
    if warm:
        bin_children = pairings[0]
        findings, record, exc = _timed_pairing(runner, cfg, ctx, roster, container, bin_children)
        if isinstance(exc, LLMUnavailableError):
            # SYSTEMIC failure (auth / key / connection / rate-limit) on the warming
            # call: the whole tier is down — abort rather than fan out N-1 doomed calls
            # (mirrors the Pass-1 tier). run_review turns this into an INDETERMINATE,
            # unsigned verdict.
            logger.warning(
                "container warm bin %s SYSTEMIC failure (%s); aborting fan-out",
                record["children"],
                record["error"],
            )
            raise exc
        if exc is not None:
            # NON-systemic warm failure: the cache prefix may not be written, so degrade
            # to a direct fan-out of ALL pairings (accept the possible all-miss) rather
            # than serialize on a broken warm — never hang. The failed pairing re-runs in
            # the pool (so it is not silently dropped here).
            logger.warning(
                "container warm bin %s failed (%s); degrading to direct fan-out",
                record["children"],
                record["error"],
            )
        else:
            warmed = True
            out.extend(findings)
            pairing_records.append(record)
            call_records.append(sizing.usage_record(record["criteria"], record["usage"]))
            logger.info(
                "container warm bin %s: %d finding(s) in %.1fs (cache warmed)",
                record["children"],
                record["findings"],
                record["seconds"],
            )
            to_pool = pairings[1:]

    # Fan out the remaining (warmed) — or all (not warmed) — pairings CONCURRENTLY in a
    # NEW pool. Per-pairing failures drop only that pairing's findings (recorded), never
    # aborting the aggregate — exactly the sequential baseline's behaviour. NOTE: unlike
    # the WARM call (which aborts on a SYSTEMIC LLMUnavailableError), a systemic failure
    # that strikes a fanned-out pairing here intentionally DEGRADES (drops that pairing)
    # rather than aborts — matching the pre-S3 per-pairing `except Exception`. In a real
    # outage the earlier Pass-1 chunk pool re-raises LLMUnavailableError before the
    # container stage is ever reached, so this path is not the outage signal.
    max_workers = max(1, min(_CONTAINER_MAX_WORKERS, len(to_pool))) if to_pool else 0
    if to_pool:
        with ThreadPoolExecutor(max_workers=max_workers) as ex:
            futs = [
                _submit_ctx(ex, _timed_pairing, runner, cfg, ctx, roster, container, bin_children)
                for bin_children in to_pool
            ]
            for fu in futs:
                findings, record, exc = fu.result()
                out.extend(findings)
                pairing_records.append(record)
                if exc is not None:
                    logger.warning(
                        "container bin %s FAILED in %.1fs (%s)",
                        record["children"],
                        record["seconds"],
                        record["error"],
                    )
                else:
                    call_records.append(sizing.usage_record(record["criteria"], record["usage"]))
                    logger.info(
                        "container bin %s: %d finding(s) in %.1fs",
                        record["children"],
                        record["findings"],
                        record["seconds"],
                    )

    container_dt = time.monotonic() - container_t0
    coverage["container"] = {
        "criteria": [c["id"] for c in container],
        "children": len(ctx.children),
        "bins": len(pairings),
        "pairings_evaluated": len(pairing_records),
        "pairings": pairing_records,
        "parallel": True,
        "warmed": warmed,
        "max_workers": max_workers,
        "total_seconds": round(container_dt, 1),
    }
    logger.info(
        "plan-review container fan-out done: %d pairing(s) in %.1fs total (parallel, warmed=%s)",
        len(pairing_records),
        container_dt,
        warmed,
    )
    return out, call_records


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


def _linked_session_log(
    ctx: PlanContext, cfg: LLMConfig, runner, usage_records: list[dict[str, Any]] | None = None
) -> tuple[str | None, bool]:
    """The text of the ticket's linked SESSION LOG(s) for the ISF criterion, and
    whether it was summarized to fit the window. Returns ``(None, False)`` when no
    session log is linked (ISF then does not run). Best-effort: any read error →
    ``(None, False)`` (ISF is skipped, never crashes the review).

    When the oversized-log SUMMARIZER runs, its call usage is appended to
    ``usage_records`` as an ISF-attributed record (``criteria=["ISF"]``) — the
    summarizer is an LLM call made solely in service of the ISF criterion."""
    from rebar import _reads

    bodies: list[str] = []
    try:
        for dep in ctx.state.get("deps", []) or []:
            tgt = dep.get("target_id")
            if not tgt:
                continue
            try:
                log = _reads.show_ticket(tgt, repo_root=ctx.tickets_root)
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
        summary, usage = passes.summarize_for_isf(runner, cfg, log_text=text)
        if usage_records is not None:
            usage_records.append(sizing.usage_record(["ISF"], usage))
        return (summary, True)
    except Exception:  # noqa: BLE001 — ISF summarization is best-effort; broad-but-logged below, ISF skipped
        # Summarization failure → ISF runs without the oversized log; log it (floor).
        logger.warning("ISF summarization failed; skipping ISF", exc_info=True)
        return (None, False)


def _ticket_size(ctx: PlanContext) -> str:
    if ctx.has_children:  # a container: halve the rubric chunk (size_factor)
        return "has_children"
    if det_floor.est_tokens(ctx.plan_text) > 8000:
        return "large"
    return "moderate"


def _no_file_impact_context(ctx: PlanContext) -> str:
    """Render the persisted no-file-impact rationale for its Pass-1 rubric."""
    reason = ctx.state.get("no_file_impact_reason")
    payload = {"declared_reason": reason if isinstance(reason, str) else ""}
    return "## Declared no-file-impact context\n" + json.dumps(
        payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True
    )


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
    # G5 decomposition signal (spangly-beggarly-blackrhino): the authoritative,
    # store-derived child summary, injected into ONLY the chunk that carries G5 (below) so
    # co-chunked criteria are unaffected. Empty when the ticket has no children.
    decomp_context = det_floor.decomposition_state_block(ctx)
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
    call_records: list[dict[str, Any]] = []
    ladder_events: list[str] = []
    # Chunk-atomic CHECKPOINTING: resume completed Pass-1 chunks from a prior run
    # (keyed by the ticket's MATERIAL fingerprint, so an edit invalidates the cache),
    # and persist each chunk's result atomically as it completes.
    material = material_fingerprint(ctx)
    resumed = 0

    def _chunk(
        chunk: list[dict], agentic: bool
    ) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        nonlocal resumed
        # Mid-run cancellation (story 2c89): once the run is cancelled (the ticket's OWN
        # material changed), a not-yet-started chunk returns empty WITHOUT a runner call
        # or checkpoint I/O — everything after the edit is waste (the checkpoints are
        # fingerprint-keyed and already orphaned). In-flight runner calls are not
        # interruptible; up to pool-width of them complete as sunk cost. The ContextVar
        # scope reaches pool workers via _submit_ctx's copy_context().
        if generation.review_cancelled():
            return [], []
        cached = sizing.load_checkpoint(ctx, material, chunk, cfg.model, agentic)
        if cached is not None:
            resumed += 1
            # A checkpoint-served chunk made NO LLM call this run — zero usage records.
            return cached, []
        # These criterion-scoped contexts compose in a stable order when G5 and
        # no-file-impact share a facet chunk: child-state first, declaration second.
        context_parts: list[str] = []
        if any(c.get("id") == "G5" for c in chunk) and decomp_context:
            context_parts.append(decomp_context)
        if any(c.get("id") == "no-file-impact" for c in chunk):
            context_parts.append(_no_file_impact_context(ctx))
        extra = "\n\n".join(context_parts)
        out, calls = _pass1_with_ladder(runner, cfg, plan, chunk, agentic, ladder_events, extra)
        sizing.save_checkpoint(ctx, material, chunk, cfg.model, agentic, out)
        return out, calls

    max_workers = max(1, min(6, len(chunks) + len(agent)))
    # WARM-THEN-FAN-OUT gate (story 25fa; ported from the container path above): a naive
    # concurrent fan-out makes the first ~max_workers calls each MISS+WRITE the
    # byte-identical plan-bearing system prefix. Only worth warming when that prefix is
    # large enough to cache (>= the cache floor) AND there is more than one Pass-1 call
    # to amortize it over AND a single-turn chunk exists to serve as the warm call.
    warm = (
        bool(chunks)
        and _prefix_tokens(plan) >= CACHE_MIN_PREFIX_TOKENS
        and len(chunks) + len(agent) >= 2
    )
    warmed = False
    # Observability: record + log how Pass-1 criteria were batched across the tiers
    # (single-turn chunks + agent criteria run CONCURRENTLY in the pool, optionally
    # behind a serial cache-warming chunk; container criteria then run as a PARALLEL
    # warm-then-fan-out afterward — see _run_container).
    # Quiet by default; enable with REBAR_LOG_LEVEL=INFO. Always recorded into coverage.
    coverage["batch_plan"] = {
        "single_turn_chunks": len(chunks),
        "agent_criteria": [c["id"] for c in agent],
        "container_criteria": [c["id"] for c in container] if container else [],
        "shed_criteria": [c["id"] for c in shed] if shed else [],
        "children": len(ctx.children) if ctx.has_children else 0,
        "max_workers": max_workers,
        "warm": warm,
    }
    logger.info(
        "plan-review pass1 batch: %d single-turn chunk(s) + %d agent criterion(s) "
        "concurrently (max_workers=%d, warm=%s); %d container criterion(s) parallel "
        "(warm-then-fan-out) over %d child(ren); %d criterion(s) shed to budget",
        len(chunks),
        len(agent),
        max_workers,
        warm,
        len(container) if container else 0,
        len(ctx.children) if ctx.has_children else 0,
        len(shed) if shed else 0,
    )
    pool_chunks = chunks
    if warm:
        # Run the FIRST single-turn chunk serially to completion so its completed call
        # WRITES the shared plan-bearing cache prefix; the remainder then fans out and
        # READS it. A SYSTEMIC LLMUnavailableError propagates from _chunk (the ladder
        # re-raises it) and aborts here rather than fanning out N-1 doomed calls —
        # exactly what the pool's own handler would do (run_review turns it into an
        # INDETERMINATE, unsigned verdict). A NON-systemic warm failure is swallowed by
        # the ladder (it returns no completed call records, dropping that chunk's
        # findings exactly as today's direct fan-out would) — the cache prefix may not
        # be written, so degrade: fan the remainder out directly, never hang or abort.
        # A checkpoint-served warm chunk likewise made no call → nothing warmed.
        warm_findings, warm_calls = _chunk(chunks[0], False)
        findings.extend(warm_findings or [])
        call_records.extend(warm_calls or [])
        pool_chunks = chunks[1:]
        warmed = bool(warm_calls)
        if warmed:
            logger.info("pass1 warm chunk done: %d finding(s) (cache warmed)", len(warm_findings))
        else:
            logger.warning(
                "pass1 warm chunk completed no LLM call (failed or checkpoint-served); "
                "degrading to direct fan-out of the remainder"
            )
    coverage["batch_plan"]["warmed"] = warmed
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        st_futs = [_submit_ctx(ex, _chunk, ch, False) for ch in pool_chunks]
        ag_futs = [_submit_ctx(ex, _chunk, [c], True) for c in agent]
        for fu in st_futs + ag_futs:
            try:
                # Each future resolves to a (findings, usage_records) pair.
                chunk_findings, chunk_calls = fu.result()
                findings.extend(chunk_findings or [])
                call_records.extend(chunk_calls or [])
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
        container_findings, container_calls = _run_container(ctx, cfg, runner, container, coverage)
        findings.extend(container_findings)
        call_records.extend(container_calls)

    # ISF (child 681b): fed the LINKED SESSION LOG (not a rubric chunk), single-turn,
    # fires ONLY when a session log is linked. Oversized logs fall back to a SUMMARY
    # (recorded; findings carry reduced confidence). The plan is never summarized.
    log_text, summarized = _linked_session_log(ctx, cfg, runner, call_records)
    if log_text:
        coverage["isf"] = {"ran": True, "summarized": summarized}
        try:
            isf_findings, isf_usage = passes.pass1_isf(
                runner,
                cfg,
                plan=plan,
                session_log_text=log_text,
                ticket_graph=_ticket_graph_blob(ctx),
                summarized=summarized,
            )
            findings.extend(isf_findings)
            call_records.append(sizing.usage_record(["ISF"], isf_usage))
        except Exception as exc:  # noqa: BLE001 — ISF pass is best-effort; broad-but-logged + recorded in coverage, never blocks the verdict
            # ISF (in-session-failure) pass is best-effort; record in-band + log (floor).
            logger.warning("ISF pass failed; verdict emitted without ISF findings", exc_info=True)
            coverage["isf"]["error"] = str(exc)
    else:
        coverage["isf"] = {"ran": False, "reason": "no linked session_log"}

    # Recall (disused-unpoliced-solenodon): re-surface prior-review findings the fresh finder
    # MISSED, as post-Pass-1 candidates for the UNCHANGED Pass-2 verifier. The finder above ran
    # WITHOUT any prior findings (independence by construction; ADR 0008 Inv. 1 / the pinned
    # test_prior_findings_only_reach_the_novelty_seam). A candidate the current plan resolved fails
    # Pass-2 validity and is dropped — recall never blocks on memory alone. Best-effort + bounded.
    concerns = sidecar.prior_concerns(ctx.ticket_id, repo_root=ctx.tickets_root)
    if concerns:
        seen = {sidecar.norm_id(f) for f in findings}
        recalled = 0
        for c in concerns:
            nid = c.get("norm_id") or sidecar.norm_id(c)
            if nid in seen:
                continue  # the fresh finder already found it (or a dup within the concern set)
            seen.add(nid)
            findings.append(
                {
                    "finding": c.get("finding", ""),
                    "suggested_fix": c.get("suggested_fix", ""),
                    "criteria": list(c.get("criteria", []) or []),
                    "location": c.get("location", ""),
                    "evidence": [],
                    "impact": "",
                    # internal observability marker (underscore-prefixed, like _too_big / _shed)
                    "_recall": True,
                }
            )
            recalled += 1
        if recalled:
            coverage["recall_candidates"] = recalled
            logger.info(
                "Pass-1 recall: re-surfaced %d missed prior finding(s) as post-Pass-1 candidates",
                recalled,
            )

    # G5 decomposition backstop (spangly-beggarly-blackrhino): if the store shows this
    # ticket HAS children, drop any residual G5 "flat/undecomposed" finding the finder
    # still emitted. Deterministic, post-Pass-1 (the only seam where the model finding and
    # the store child-count are co-observable — see det_floor); child content/altitude G5
    # findings are preserved.
    findings, vetoed_g5 = det_floor.veto_undecomposed_g5(findings, ctx)
    if vetoed_g5:
        coverage["g5_undecomposed_vetoed"] = len(vetoed_g5)
        logger.info(
            "G5 decomposition veto: dropped %d false 'undecomposed' finding(s) "
            "(ticket has %d store child(ren))",
            len(vetoed_g5),
            len(ctx.children),
        )

    # Per-call usage aggregation (story d52a): the raw call records + the derived
    # per-criterion map + the totals, recorded into coverage so the batch runner can
    # emit them (and merge the prerequisite finder's usage) as outputs["_usage"].
    coverage["usage"] = aggregate_usage(call_records)

    return findings


def aggregate_usage(per_call: list[dict[str, Any]]) -> dict[str, Any]:
    """Aggregate Pass-1 per-CALL usage records (story d52a) into
    ``{"per_call": [...], "per_criterion": {...}, "totals": {...}}``.

    Each input record is one COMPLETED LLM call (:func:`sizing.usage_record`):
    ``{criteria, requests, input_tokens, output_tokens, cache_read_tokens,
    cache_write_tokens}`` — deliberately with NO per-record ``llm_calls`` field (one
    record IS one call). The per-criterion ``llm_calls`` is DERIVED here as the count
    of records covering that criterion.

    Attribution rule: an AGENT call (single criterion) attributes wholly to that
    criterion; a multi-criterion single-turn chunk divides its TOKEN fields equally
    across its covered criteria — a documented approximation (the sidecar retains the
    raw per-call records, so exact attribution is always reconstructable) — while the
    ``requests`` field and the derived ``llm_calls`` count IN FULL for each covered
    criterion.

    Known limitation (documented, out of scope to fix): an attempt that RAISES (e.g.
    the context-limit error that triggers the size-ladder escalation) returns no
    result dict, so its usage is not recoverable from the current runner API — such
    attempts appear in no record and contribute nothing. Likewise a checkpoint-served
    chunk made no call this run and contributes nothing."""
    totals: dict[str, Any] = {
        "llm_calls": len(per_call),
        "requests": 0,
        **{f: 0 for f in sizing.USAGE_TOKEN_FIELDS},
    }
    per_criterion: dict[str, dict[str, Any]] = {}
    for rec in per_call:
        totals["requests"] += int(rec.get("requests", 0) or 0)
        for f in sizing.USAGE_TOKEN_FIELDS:
            totals[f] += int(rec.get(f, 0) or 0)
        crits = [str(c) for c in (rec.get("criteria") or [])]
        n = len(crits)
        for cid in crits:
            slot = per_criterion.setdefault(
                cid,
                {"llm_calls": 0, "requests": 0, **{f: 0 for f in sizing.USAGE_TOKEN_FIELDS}},
            )
            slot["llm_calls"] += 1  # derived: the count of covering call records
            slot["requests"] += int(rec.get("requests", 0) or 0)
            for f in sizing.USAGE_TOKEN_FIELDS:
                slot[f] += int(rec.get(f, 0) or 0) / n  # equal split across criteria
    return {
        "per_call": [dict(r) for r in per_call],
        "per_criterion": per_criterion,
        "totals": totals,
    }


def material_fingerprint(ctx: PlanContext, *, normalize_checkboxes: bool = True) -> str:
    """A hash of the ticket's MATERIAL plan content (description / AC / file_impact /
    decomposition) — bound into the attestation so a material edit invalidates the
    signature. Tags/comments/links/assignee are NOT material (excluded), and neither
    is AC checkbox STATE (normalized away — see :func:`material_diff.normalize_checkbox_state`).

    ``normalize_checkboxes=False`` reproduces the PRE-normalization (pre-330c) algorithm
    that hashed the raw description. Validity-on-read uses it as a grandfather fallback
    (bug 96d1): a signed hash that byte-exactly matches the legacy recomputation proves
    the material is unchanged — not even box state moved — so a pre-330c attestation over
    an unedited ticket is not spuriously invalidated as stale-material.

    The BASIS itself lives in :func:`material_diff.material_basis`, which also hashes each
    key separately so a staleness message can name WHICH component moved (bug 94a3). One
    definition of "material" keeps the composite and the per-component view in agreement.
    """
    from .material_diff import material_basis

    basis = material_basis(ctx, normalize_checkboxes=normalize_checkboxes)
    blob = json.dumps(basis, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()[:16]
