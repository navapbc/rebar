"""Production batch-runner for the plan-review gate (epic B, story B1 "part 2").

A THIN adapter that plugs plan-review's adaptive Pass-1 finder machinery into the
generic workflow ``BatchRunner`` seam (:mod:`rebar.llm.workflow.runners`). It owns
NO sizing/loop/budget logic — it maps a generic :class:`BatchRunRequest` onto the
SHARED units the bespoke orchestrator also calls
(:func:`.orchestrator.assemble_context`, :mod:`.registry`, :func:`.pass1.run_pass1`),
so there is no duplicated algorithm (B1 AC3). See
``docs/design/batch-runner-seam.md`` (decisions D1-D5).

Key design points it embodies:

* **D1 — generic seam, runner reconstructs context.** ``BatchRunRequest`` stays
  plan-review-agnostic; the runner re-derives the whole :class:`PlanContext` from
  ``req.target_ticket`` + ``req.repo_root`` (cheap local reads; replay-safe because the
  interpreter journals an opaque plan and never re-runs the runner on replay).
* **D3 (reframed) — the runner owns an INJECTABLE ``rebar.llm.Runner``.** Plan-review's
  finder drives a ``rebar.llm.Runner`` directly (not a generic workflow agent step), so
  the seam's ``agent_runner`` is intentionally UNUSED. The injected runner is the
  offline/parity-test seam (B4 passes a fake ``rebar.llm.Runner``); when absent it is
  constructed per-run via :func:`get_runner`.
* **D4 — budget.** The per-plan cap is computed inside ``run_pass1`` (via
  ``sizing.plan_budget_cap``); ``req.usd_budget`` is meant to override it. There is no
  clean override seam in today's ``pass1``/``sizing`` API (see :meth:`run`), so the
  requested override is journaled and the computed cap is used — a documented follow-up.
* **D5 — prompt-id IS the registry id.** Each ``criteria`` entry's ``prompt`` is its
  registry criterion id; the runner resolves descriptors via ``registry.by_id()`` and
  splits single/agent by ``registry.exec_tier`` (NOT ``route_criteria`` — ``req.criteria``
  is already the INCLUDED set; ``run_pass1`` itself pulls the container criteria out).
"""

from __future__ import annotations

import dataclasses
import logging
from collections.abc import Mapping
from typing import Any

from rebar.llm.config import LLMConfig
from rebar.llm.runner import Runner, get_runner
from rebar.llm.workflow.runners import (
    AgentStepRunner,
    BatchRunner,
    BatchRunRequest,
    BatchRunResult,
)

from . import registry
from .orchestrator import assemble_context
from .pass1 import run_pass1

logger = logging.getLogger(__name__)


class ProductionBatchRunner(BatchRunner):
    """The plan-review production :class:`BatchRunner`: reconstruct the ticket's
    :class:`PlanContext`, resolve + tier-split the included criteria, and drive the
    shared :func:`.pass1.run_pass1` adaptive finder loop, journaling its opaque
    ``coverage`` plan. A thin glue layer — all sizing/budget/ladder/checkpoint logic
    lives in the shared ``pass1``/``sizing`` units, not here."""

    def __init__(self, *, runner: Runner | None = None) -> None:
        # The INJECTABLE rebar.llm.Runner (D3). None → constructed per-run via
        # get_runner(cfg); injection is the offline/parity-test seam.
        self._runner = runner

    def run(
        self, req: BatchRunRequest, agent_runner: AgentStepRunner | None = None
    ) -> BatchRunResult:
        # The seam passes ``agent_runner`` (and the interpreter calls run(req, rc.runner)),
        # but plan-review's finder drives a rebar.llm.Runner directly, so it is unused (D3).
        del agent_runner

        # target_ticket guard (post-brainstorm critique): a production-runner batch step
        # always runs against a target ticket (the claim gate always has one).
        if not req.target_ticket:
            raise ValueError(
                "ProductionBatchRunner requires req.target_ticket: the plan-review batch "
                "reconstructs the PlanContext from the target ticket, so it cannot run "
                "without one (got None)."
            )

        # D1: reconstruct the whole-ticket context from the generic request.
        ctx = assemble_context(req.target_ticket, repo_root=req.repo_root)

        # The Pass-1 ENTRY model: model_ladder[0] if supplied (run_pass1's size ladder
        # escalates up from here). Everything else comes from the environment/config.
        cfg = LLMConfig.from_env(repo_root=req.repo_root)
        if req.model_ladder:
            cfg = dataclasses.replace(cfg, model=req.model_ladder[0])

        # D5: resolve each criterion's descriptor by its prompt-id and split by tier.
        single, agent, skipped = _resolve_criteria(req.criteria)

        runner = self._runner or get_runner(cfg)

        # ``coverage`` IS the journaled OPAQUE plan (budget/shed/ladder/checkpoint),
        # filled in by run_pass1. We seed it with the resolution record (which criteria
        # landed in which tier / were skipped) for observability — the interpreter stores
        # the whole dict but never branches on its internals.
        coverage: dict[str, Any] = {
            "batch_resolution": {
                "single": [c["id"] for c in single],
                "agent": [c["id"] for c in agent],
                "skipped": skipped,
            }
        }

        # D4 budget override: there is NO clean seam in today's pass1/sizing API to
        # override the per-plan cap — `run_pass1` calls `sizing.shed_to_budget`, which
        # computes the cap internally via `sizing.plan_budget_cap(ctx)` (reading
        # REBAR_PLAN_REVIEW_BUDGET as the *base*, then centrality-scaling it), with no
        # injection point. The only "overrides" available are (a) mutating that env var —
        # which is process-global AND centrality-scaled, so it is NOT a true cap override —
        # or (b) adding a cap-override parameter to shed_to_budget/run_pass1, which is gate
        # code outside this thin adapter. Rather than hack it, we JOURNAL the requested
        # override and fall back to the computed cap.
        # FOLLOW-UP: thread an explicit `cap_override` through run_pass1 → shed_to_budget.
        if req.usd_budget is not None:
            coverage["requested_usd_budget"] = req.usd_budget
            coverage["budget_override_applied"] = False
            logger.warning(
                "ProductionBatchRunner: req.usd_budget=%s requested but there is no clean "
                "cap-override seam in pass1/sizing yet; using the computed plan_budget_cap "
                "(documented follow-up).",
                req.usd_budget,
            )

        findings = run_pass1(ctx, cfg, runner, single, agent, coverage)
        return BatchRunResult(
            outputs={
                "findings": findings,
                "criteria_count": len(req.criteria),
                "batch_plan": coverage,
            }
        )


def _resolve_criteria(
    criteria: tuple[Mapping[str, Any], ...],
) -> tuple[list[dict], list[dict], list[str]]:
    """Resolve each included criterion's registry descriptor by its ``prompt`` id and
    split into ``(single, agent)`` by ``registry.exec_tier == "AGENT"`` (the container
    criteria G3/G4 are pulled out of ``agent`` by ``run_pass1`` itself). Ids absent from
    the registry are collected into ``skipped`` and ignored (logged), never fatal.

    NOTE: this does NOT re-apply ``route_criteria``'s ``applies()``/overlay filtering —
    ``req.criteria`` is the already-INCLUDED set (the interpreter resolved each ``when``
    before building the request)."""
    by_id = registry.by_id()
    single: list[dict] = []
    agent: list[dict] = []
    skipped: list[str] = []
    for entry in criteria:
        cid = entry.get("prompt")
        desc = by_id.get(cid)
        if desc is None:
            skipped.append(cid)
            logger.warning(
                "ProductionBatchRunner: criterion %r is not in the criteria registry; skipping",
                cid,
            )
            continue
        if registry.exec_tier(desc) == "AGENT":
            agent.append(desc)
        else:
            single.append(desc)
    return single, agent, skipped


__all__ = ["ProductionBatchRunner"]
