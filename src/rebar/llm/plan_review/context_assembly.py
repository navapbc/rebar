"""Run-scoped plan-review context assembly (ticket 1484).

Reads a ticket graph — the plan plus its direct children, each whole — into a
:class:`~rebar.llm.plan_review.det_floor.PlanContext`, and memoizes that read for the extent of
one gate run so the four-pass workflow's repeated identical calls collapse to ONE N+1 store read.

Extracted from ``orchestrator`` because that module sat at 796 LOC against the 800-LOC hard cap
while ``finalize_verdict`` — which story 343b must add a parameter to — lives there.
``orchestrator`` re-imports every name below, so they remain ITS module-globals; the
``from .orchestrator import assemble_context`` in ``production_batch_runner`` and in four test
modules keeps resolving with no edit. Same zero-test-edit mechanism as tasks 2682 and 3a98.

STRICT LEAF: every import here is a sibling leaf (``config``, ``det_floor``, ``sizing``) or lazy
inside a body (``rebar._reads``); none of them import ``orchestrator``, so no cycle is possible.
"""

from __future__ import annotations

import contextlib
import contextvars
import logging
import time
from collections.abc import Iterator
from typing import Any

from rebar.llm.config import LLMConfig
from rebar.llm.gate_context import current_code_root, current_tickets_root, resolve_code_root

from .budget import centrality as _centrality
from .det_floor import PlanContext
from .sizing import largest_window_tokens

logger = logging.getLogger(__name__)

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
    hierarchy_incomplete = False
    hierarchy_incomplete_detail: list[str] = []
    # Bounded retry (2 attempts total, small fixed delay) around both the enumeration read and
    # each per-child fetch — a transient store hiccup should not silently degrade P5/P8 coverage
    # when a single retry would have succeeded. Same broad `except Exception` predicate as before
    # (a store read can fail in many shapes); only the retry wrapping + failure bookkeeping is new.
    listed: list[dict[str, Any]] = []
    _RETRY_ATTEMPTS = 2
    _RETRY_DELAY_S = 0.05
    enumerated = False
    for attempt in range(_RETRY_ATTEMPTS):
        try:
            listed = _reads.list_tickets(parent=canonical, repo_root=repo_root) or []
            enumerated = True
            break
        except Exception:
            if attempt + 1 < _RETRY_ATTEMPTS:
                time.sleep(_RETRY_DELAY_S)
                continue
            # Failing to enumerate children degrades P5/P8 coverage — a real signal, logged.
            logger.warning(
                "could not list children of %s; reviewing without", canonical, exc_info=True
            )
            listed = []
    if not enumerated:
        hierarchy_incomplete = True
        hierarchy_incomplete_detail.append("enumeration")
    for c in listed:
        cid = c.get("ticket_id")
        if cid is None:
            children.append(c)
            continue
        fetched = False
        for attempt in range(_RETRY_ATTEMPTS):
            try:  # fetch full child state (deps + file_impact) for P5/P8
                children.append(_reads.show_ticket(cid, repo_root=repo_root))
                fetched = True
                break
            except Exception:  # noqa: BLE001 — per-child best-effort full-state fetch; fall back to the summary
                if attempt + 1 < _RETRY_ATTEMPTS:
                    time.sleep(_RETRY_DELAY_S)
                    continue
                children.append(c)
        if not fetched:
            hierarchy_incomplete = True
            hierarchy_incomplete_detail.append(str(cid))
    return PlanContext(
        ticket_id=canonical,
        ticket_type=state.get("ticket_type", ""),
        title=state.get("title", ""),
        description=state.get("description", ""),
        state=state,
        children=children,
        hierarchy_incomplete=hierarchy_incomplete,
        hierarchy_incomplete_detail=hierarchy_incomplete_detail,
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
