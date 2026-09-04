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
  ``sizing.plan_budget_cap``), and ``req.usd_budget`` overrides it through the explicit
  ``cap_override`` seam threaded ``run_pass1`` → ``sizing.shed_to_budget``. The override is
  the FINAL cap, used verbatim rather than centrality-scaled (see :meth:`run`); ``None``
  leaves the computed cap in force.
* **D5 — prompt-id IS the registry id.** Each ``criteria`` entry's ``prompt`` is its
  registry criterion id; the runner resolves descriptors via ``registry.by_id()`` and
  splits single/agent by ``registry.exec_tier`` (NOT ``route_criteria`` — ``req.criteria``
  is already the INCLUDED set; ``run_pass1`` itself pulls the container criteria out).
"""

from __future__ import annotations

import dataclasses
import logging
from collections.abc import Iterable, Mapping
from typing import Any, cast

from rebar.llm.config import resolve_gate_config
from rebar.llm.model_classes import resolve_model_string
from rebar.llm.runner import Runner, get_runner
from rebar.llm.workflow.runners import (
    AgentStepRunner,
    BatchRunner,
    BatchRunRequest,
    BatchRunResult,
)

from . import registry, sizing
from .orchestrator import assemble_context, route_criteria
from .pass1 import aggregate_usage, run_pass1
from .registry import _PROJECT_PREFIX, _PROMPT_ID_PREFIX

logger = logging.getLogger(__name__)


class ProductionBatchRunner(BatchRunner):
    """The plan-review production :class:`BatchRunner`: reconstruct the ticket's
    :class:`PlanContext`, resolve + tier-split the included criteria, and drive the
    shared :func:`.pass1.run_pass1` adaptive finder loop, journaling its opaque
    ``coverage`` plan. A thin glue layer — all sizing/budget/ladder/checkpoint logic
    lives in the shared ``pass1``/``sizing`` units, not here."""

    def __init__(self, *, runner: Runner | None = None, tf_provider: Any = None) -> None:
        # The INJECTABLE rebar.llm.Runner (D3). None → constructed per-run via
        # get_runner(cfg); injection is the offline/parity-test seam.
        self._runner = runner
        # REB-640: the Terraform Pass-1 tool hook (terraform_seam.pass1_tool_hook), threaded
        # into run_pass1 so the T10 AGENTIC finder — which this runner drives directly, past
        # the discarded ``agent_runner`` — gets its grounding tools via RunRequest.extra_tools.
        # None for every non-Terraform review (byte-identical to before).
        self._tf_provider = tf_provider

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
        # escalates up from here). Everything else comes from the CALLER-RESOLVED run config
        # (veiny-trout-brink) — resolve_gate_config returns the gate-run cfg when inside a run
        # (this batch runner is NOT a workflow step, so it cannot read ctx.inputs), else from_env.
        # The rung is resolved through the MODEL CLASS table (task 7761): the ladder names
        # classes (`trivial`/`standard`/`frontier`), and copying a rung verbatim onto cfg.model
        # is exactly what sent Pass-1 — 41 of 42 calls on a real review — to direct Anthropic on
        # a Bedrock-configured run. A non-class string is returned unchanged.
        cfg = resolve_gate_config(req.repo_root)
        if req.model_ladder:
            cfg = dataclasses.replace(
                cfg, model=resolve_model_string(req.model_ladder[0], req.repo_root)
            )

        # D5: resolve each criterion's descriptor by its prompt-id and split by tier.
        single, agent, skipped = _resolve_criteria(req.criteria)

        # Project fan-in (epic 3156): activated `project.<name>` criteria have NO static YAML
        # `criteria` slot (the v3 `batch` schema is immutable), so they are added HERE — routed
        # through the SAME route_criteria applies()/overlay filter the built-ins use, keyed off
        # ctx.repo_root (== the assemble step's root, so the vocab never diverges). Built-ins
        # still come from req.criteria (the interpreter's when/probe-filtered set); only
        # `project.`-prefixed ids are appended, deduped against the built-in set. Under PROBE
        # MODE the allowlist arrives on the batch step's `with:` (the generic `with_inputs`
        # seam) and filters this fan-in too, so a probe stays as cheap as its allowlist.
        proj_single, proj_agent = _project_criteria(
            ctx,
            {c["id"] for c in (*single, *agent)},
            req.with_inputs.get("probe_criteria"),
        )
        single.extend(proj_single)
        agent.extend(proj_agent)

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
                "project": [c["id"] for c in (*proj_single, *proj_agent)],
            }
        }

        # D4 budget override: `req.usd_budget` is the caller's explicit per-plan cap, applied
        # through the `cap_override` seam on run_pass1 → sizing.shed_to_budget. It is used
        # VERBATIM — deliberately NOT centrality-scaled, unlike REBAR_PLAN_REVIEW_BUDGET, which
        # plan_budget_cap reads as the *base* before scaling and so can never mean "this run
        # costs at most $X". Absent (None) → the computed, centrality-scaled cap, unchanged.
        if req.usd_budget is not None:
            coverage["requested_usd_budget"] = req.usd_budget
            coverage["budget_override_applied"] = True

        findings = run_pass1(
            ctx, cfg, runner, single, agent, coverage, req.usd_budget, self._tf_provider
        )
        prerequisite_coverage: list[dict[str, Any]] = []
        prerequisite_findings: list[dict[str, Any]] = []
        snapshot_value = req.with_inputs.get("relation_snapshot")
        if hasattr(snapshot_value, "prerequisite_ids"):
            snapshot = cast(Any, snapshot_value)
            blocks = [
                {
                    "canonical_id": pid,
                    "rendered_text": str(snapshot.ticket_states_by_id[pid].get("description", "")),
                }
                for pid in snapshot.prerequisite_ids
            ]
        else:
            blocks = list(snapshot_value or req.with_inputs.get("prerequisites") or [])
        if blocks:
            from .prerequisites import PREREQUISITE_CRITERION, run_focused_finder

            prerequisite_coverage, prerequisite_findings, prerequisite_usage = run_focused_finder(
                runner,
                cfg,
                subject_plan=str(req.with_inputs.get("subject_plan", ctx.plan_text)),
                blocks=blocks,
                ticket_id=str(req.target_ticket or ""),
            )
            # Merge the prerequisite finder's summed usage into the Pass-1 aggregate
            # (story d52a): one call record attributed to the prerequisite criterion,
            # appended to run_pass1's per-call records, then re-aggregated. Skipped when
            # zero (no runner.run made it to usage — e.g. every bin oversized).
            if any(prerequisite_usage.values()):
                usage = coverage.get("usage") or aggregate_usage([])
                per_call = [
                    *usage.get("per_call", []),
                    sizing.usage_record([PREREQUISITE_CRITERION], prerequisite_usage),
                ]
                coverage["usage"] = aggregate_usage(per_call)
        return BatchRunResult(
            outputs={
                "findings": findings,
                "prerequisite_coverage": prerequisite_coverage,
                "prerequisite_findings": prerequisite_findings,
                "has_prerequisites": bool(prerequisite_coverage),
                "criteria_count": len(req.criteria),
                "batch_plan": coverage,
                # The Pass-1 + prerequisite usage aggregate (story d52a): raw per-call
                # records, the derived per-criterion map, and the totals — the payload
                # _attach_plan_review_metrics folds into coverage.metrics/coverage.usage.
                "_usage": coverage.get("usage") or aggregate_usage([]),
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
    before building the request). It DOES mirror ``route_criteria``'s ISF exclusion (ISF
    is fed the linked session log by ``run_pass1`` itself, never a rubric chunk — listing
    it as a batch criterion would double-evaluate it) and dedupes repeated ids."""
    by_id = registry.by_id()
    single: list[dict] = []
    agent: list[dict] = []
    skipped: list[str] = []
    seen: set[str] = set()
    for entry in criteria:
        cid = entry.get("prompt")
        # A batch criterion's `prompt` is the prompt-library id (`plan-review-E1`) — the
        # lint-resolvable form the v3 workflow authors and the form the reference
        # DefaultBatchRunner uses — OR the bare registry id (`E1`). Normalize to the bare
        # registry id (the `registry.by_id()` key) by stripping the `plan-review-` prefix.
        if isinstance(cid, str) and cid.startswith(_PROMPT_ID_PREFIX):
            cid = cid[len(_PROMPT_ID_PREFIX) :]
        if not isinstance(cid, str) or not cid or cid in seen:
            # missing/empty/non-str id, or a duplicate — skip (duplicates would
            # double-evaluate; a missing id is a malformed criterion).
            if isinstance(cid, str) and cid:
                continue  # benign duplicate, already routed
            logger.warning("ProductionBatchRunner: criterion has no usable `prompt` id; skipping")
            continue
        seen.add(cid)
        # ISF is fed the linked session log via run_pass1's own path (mirrors
        # route_criteria:136-137); routing it as a rubric chunk would evaluate it twice.
        if cid == "ISF":
            continue
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


def _project_criteria(
    ctx, exclude: set[str], probe: Iterable[Any] | None = None
) -> tuple[list[dict], list[dict]]:
    """Fan in the ACTIVATED project criteria for the ticket (epic 3156). ``route_criteria``
    already returns the FULL routed set (built-ins ∪ activated `project.<name>` criteria, each
    past its ``applies()``/overlay filter); this picks out ONLY the ``project.``-prefixed ones
    (deduped against the already-resolved built-in set), tier-split exactly as route_criteria
    did. Built-ins are intentionally NOT taken from here — they come from ``req.criteria`` so the
    interpreter's per-criterion ``when``/probe gating is preserved.

    ``probe`` is the PROBE MODE (drift-refresh) allowlist, delivered from the gate's
    ``probe_criteria`` input through the batch step's ``with:`` (``BatchRunRequest.with_inputs``,
    the generic seam — no plan-review concept on the request dataclass). ``route_criteria`` has
    no probe notion of its own, so without this an activated project criterion would be
    evaluated during a probe that is meant to run only the cheap E4+G1G2 set. When non-empty the
    fan-in is filtered to ids IN the allowlist, mirroring ``workflow_ops``' built-in probe
    filter; empty/absent leaves the normal full-review fan-in untouched.

    No ``gate_log`` is passed (ticket 4ee2): this re-routes the same ticket the assemble
    step already routed, so recording its deterministic-gate skips here would duplicate
    the assemble step's ``coverage.routing.det_gated`` record — skips are captured once,
    at the assemble step's ``route_criteria`` call."""
    single, agent = route_criteria(ctx)
    allowlist = {str(c) for c in (probe or ())}

    def _keep(c: dict) -> bool:
        cid = str(c["id"])
        if not cid.startswith(_PROJECT_PREFIX) or c["id"] in exclude:
            return False
        return cid in allowlist if allowlist else True

    return [c for c in single if _keep(c)], [c for c in agent if _keep(c)]


__all__ = ["ProductionBatchRunner"]
