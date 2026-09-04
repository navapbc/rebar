"""The plan-review container (G3/G4) pairing stage, extracted from :mod:`.pass1`.

The container criteria (parent + one child at a time) run as their OWN concurrent
warm-then-fan-out loop, separate from the facet-chunked Pass-1 finder. This module holds
that loop and its helpers; :func:`.pass1.run_pass1` calls :func:`_run_container` at its
single ``if container and ctx.has_children:`` seam, and :mod:`.pass1` re-exports the public
names so the historical ``pass1.<symbol>`` call sites keep working.

Direction is one-way — ``pass1 -> container_stage -> generation``: the pool submit helper
:func:`generation._submit_ctx` captures the generation-owned cancel scope. This module must
NOT import from :mod:`.pass1` (that would create an import cycle)."""

from __future__ import annotations

import logging
import time
from concurrent.futures import ThreadPoolExecutor
from typing import Any

from rebar._plan_clarity import evaluate_plan_clarity
from rebar.llm import capabilities
from rebar.llm.config import LLMConfig
from rebar.llm.errors import LLMInputRejectedError, LLMUnavailableError
from rebar.llm.runner import Runner

from . import budget as _budget
from . import det_floor, passes, sizing
from .det_floor import PlanContext
from .generation import _submit_ctx

logger = logging.getLogger(__name__)


# Container criteria (parent + one child at a time); handled by the dedicated
# per-child loop, never the normal agent path.
CONTAINER_CRITERIA = ("G3", "G4", "decomp-shape")

# The minimum prompt-prefix the anthropic cache will write/read. Re-derived from the SAME
# single source :mod:`.pass1` reads (``llm.capabilities``), so the container warm gate and the
# Pass-1 warm gate share ONE definition of the floor rather than a second literal.
CACHE_MIN_PREFIX_TOKENS = capabilities.CACHE_MIN_PREFIX_TOKENS

# Concurrency cap for the container fan-out pool (a NEW pool — the Pass-1 pool is closed
# by the time the container criteria run).
_CONTAINER_MAX_WORKERS = 6


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
    budget = _budget.container_budget(ctx.largest_window_tokens)
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
    pairings, oversized = _budget.pack_container_bins(ctx.children, parent_tokens, budget)
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
        if isinstance(exc, (LLMUnavailableError, LLMInputRejectedError)):
            # SYSTEMIC failure (auth / key / connection / rate-limit) on the warming
            # call: the whole tier is down — abort rather than fan out N-1 doomed calls
            # (mirrors the Pass-1 tier). run_review turns this into an INDETERMINATE,
            # unsigned verdict.
            # LLMInputRejectedError (bug 43d4) aborts for the SAME cost reason rather than
            # the availability one: a container prompt the provider rejected as too large or
            # refused is rejected identically for every remaining pairing, so fanning out
            # would buy N-1 guaranteed failures. Behaviour-preserving — this arm caught a
            # rejected input before the type existed.
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
