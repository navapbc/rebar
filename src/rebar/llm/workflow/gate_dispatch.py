"""Gate-engine dispatch: PRODUCE the gate verdicts via the v3 engine workflows
(epic B, story B5 — the cutover).

The plan-review claim gate and the completion close gate each have a *verdict
production* step and a *signing* step. This module owns ONLY verdict production via
the engine workflows (``gates/plan-review.yaml`` + ``gates/completion-verification.yaml``);
the SIGNING wrappers (``review_plan`` / ``_commands.transition``) are left untouched, so
the signed attestations stay byte-compatible regardless of which engine produced the
verdict (the cutover requirement).

Degradation semantics it guarantees:

* **Plan-review INDETERMINATE-on-outage.** A systemic LLM outage (preflight raises
  :class:`LLMUnavailableError`) — or any mid-run LLM-tier failure — degrades to an
  unsigned INDETERMINATE verdict, never a hollow PASS (bug ``fuel-posse-ball``).
* **Completion fail-closed-on-outage.** The completion verifier preflights and lets
  :class:`LLMUnavailableError` PROPAGATE (the close gate catches it and fail-closes),
  and consumes the cfg the caller already tuned (verifier model + step-budget floor).

The workflow runs IN-MEMORY (``MemoryRecorder``) so a gate run writes NO workflow-run
events to the gated ticket — it only emits a sidecar / signs. The plan-review batch is
driven by the B1 ``ProductionBatchRunner``; agent steps (verify/coach) run through the
``RunnerAgentStep`` bridge.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from rebar.llm.errors import LLMUnavailableError


def _gate_doc(name: str, repo_root) -> dict[str, Any]:
    """Load a packaged gate workflow (``src/rebar/llm/workflow/gates/<name>.yaml``).

    The gate workflows are PACKAGE data, not under ``.rebar/workflows`` / ``examples``,
    so they are resolved by absolute path (not via the name-resolver)."""
    from .runs import load_workflow_doc

    p = Path(__file__).resolve().parent / "gates" / f"{name}.yaml"
    return load_workflow_doc(p, repo_root)


# ── plan-review ───────────────────────────────────────────────────────────────────
def produce_plan_review_verdict(
    ctx, cfg, *, runner=None, advisory_cap: int, repo_root=None
) -> dict[str, Any]:
    """Produce a ``plan_review_verdict`` by running ``gates/plan-review.yaml`` in-memory.

    The verdict-production half of ``review_plan``. Preflights the runner so a systemic
    outage degrades to INDETERMINATE (unsigned) before any billable call; a mid-run
    LLM-tier failure degrades the same way (never a hollow PASS)."""
    from rebar.llm.plan_review.production_batch_runner import ProductionBatchRunner
    from rebar.llm.runner import get_runner

    from . import executor as _ex
    from .recorder import MemoryRecorder
    from .runs import RunnerAgentStep

    runner_sel = runner or get_runner(cfg)
    try:
        runner_sel.preflight()
    except LLMUnavailableError as exc:
        return _degraded_plan_review_verdict(
            ctx, cfg, error=exc, advisory_cap=advisory_cap, runner_name=runner_sel.name
        )

    doc = _gate_doc("plan-review", repo_root)
    rec = MemoryRecorder()
    try:
        res = _ex.run_workflow(
            doc,
            {"ticket_id": ctx.ticket_id},
            target_ticket=ctx.ticket_id,
            repo_root=repo_root,
            agent_runner=RunnerAgentStep(runner=runner_sel, repo_root=repo_root, config=cfg),
            batch_runner=ProductionBatchRunner(runner=runner_sel),
            recorder=rec,
        )
    except LLMUnavailableError as exc:
        return _degraded_plan_review_verdict(
            ctx, cfg, error=exc, advisory_cap=advisory_cap, runner_name=runner_sel.name
        )

    verdict = res.terminal_output
    if res.status == "succeeded" and isinstance(verdict, dict) and "verdict" in verdict:
        return verdict

    # The run failed mid-tail. Pass-4 coach is advisory POLISH — bespoke run_review treats a
    # coach failure as NON-fatal (it still emits the verdict, sans coaching). Mirror that: if
    # Pass-3 `decide` succeeded (so finders+verify ran), reconstruct the verdict from the
    # decide partition with empty coaching — NOT a hollow INDETERMINATE that would discard the
    # real findings and wrongly block the claim.
    recovered = _recover_plan_review_coach_failure(rec, cfg, error=res.error)
    if recovered is not None:
        return recovered

    # finders/verify failed (the LLM tier did not produce findings) — degrade to INDETERMINATE,
    # never sign a hollow PASS, mirroring run_review's broad-except → llm_unavailable path.
    return _degraded_plan_review_verdict(
        ctx,
        cfg,
        error=(res.error or "plan-review workflow LLM tier failed"),
        advisory_cap=advisory_cap,
        runner_name=runner_sel.name,
    )


def _recover_plan_review_coach_failure(rec, cfg, *, error) -> dict[str, Any] | None:
    """If the only failure was in the Pass-4 coach tail (Pass-3 ``decide`` succeeded),
    reassemble the verdict from the recorded ``decide`` partition with EMPTY coaching —
    the same non-fatal-coach result bespoke run_review emits. Returns None if ``decide``
    did not succeed (then the LLM tier genuinely failed → caller degrades to INDETERMINATE)."""
    from rebar.llm import findings as _findings
    from rebar.llm.plan_review import orchestrator
    from rebar.llm.plan_review.det_floor import PlanContext

    # Latest-wins map of succeeded step outputs by their step id (frame-key tail).
    succeeded: dict[str, dict] = {}
    for s in rec.steps:
        if s.get("status") != "succeeded":
            continue
        fk = s.get("frame_key") or s.get("step_id") or ""
        succeeded[str(fk).rsplit("/", 1)[-1]] = s.get("outputs") or {}

    decide = succeeded.get("decide")
    precheck = succeeded.get("precheck")
    if not decide or not precheck or "blocking" not in decide:
        return None  # Pass-3 did not complete → the LLM tier failed, not just the coach

    parts = {
        k: list(decide.get(k) or [])
        for k in ("blocking", "surfaced", "overflow", "indeterminate", "dropped")
    }
    coverage = {
        "det": precheck.get("det_coverage") or {},
        "routing": (succeeded.get("assemble") or {}).get("routing") or {},
        "llm_ran": True,
        "coach_error": str(error) if error else "pass-4 coach failed; verdict emitted without it",
    }
    pctx = PlanContext(
        ticket_id=str(precheck.get("canonical_id") or ""),
        ticket_type=str(precheck.get("ticket_type") or ""),
        title="",
        description="",
    )
    verdict = orchestrator.finalize_verdict(
        pctx, parts, coaching=[], coverage=coverage, runner_name=cfg.runner, model=cfg.model
    )
    return _findings.validate_structured(verdict, "plan_review_verdict")


def _degraded_plan_review_verdict(
    ctx, cfg, *, error, advisory_cap: int, runner_name: str | None
) -> dict[str, Any]:
    """The unsigned INDETERMINATE verdict a systemic LLM outage degrades to — the SAME
    shape ``run_review`` produces (DET floor ran, LLM did not): DET findings partitioned,
    ``coverage.llm_unavailable=True`` (so ``finalize_verdict`` ⇒ INDETERMINATE and
    ``review_plan`` never signs it)."""
    from rebar.llm.plan_review import det_floor, orchestrator

    det_results = det_floor.run_det_floor(ctx)
    det_blocks = det_floor.det_blocking_findings(det_results)
    det_advisories = det_floor.det_advisory_findings(det_results)
    coverage = {
        "det": det_floor.det_coverage(det_results),
        "llm_ran": False,
        "llm_unavailable": True,
        "llm_error": str(error),
    }
    parts = orchestrator.partition_findings(
        det_blocks, det_advisories, [], advisory_cap=advisory_cap
    )
    return orchestrator.finalize_verdict(
        ctx, parts, coaching=[], coverage=coverage, runner_name=runner_name, model=cfg.model
    )


# ── completion ──────────────────────────────────────────────────────────────────────
def produce_completion_verdict(
    ticket_id: str, *, graph: bool, repo_root=None, cfg, runner=None
) -> dict[str, Any]:
    """Produce a ``completion_verdict`` by running ``gates/completion-verification.yaml``.

    The verdict-production half of ``completion.verify_completion``. The caller has already
    tuned ``cfg`` (verifier model + step floor) and resolved ``graph``; the workflow's own
    ``completion_precheck`` op runs the deterministic child-closure check, then the agentic
    verify + reconcile produce the terminal verdict. Preflights and lets
    :class:`LLMUnavailableError` PROPAGATE so the close gate fail-closes."""
    from rebar.llm.runner import get_runner

    from . import executor as _ex
    from .recorder import MemoryRecorder
    from .runs import RunnerAgentStep

    runner_sel = get_runner(cfg, override=runner)
    runner_sel.preflight()  # raises LLMUnavailableError → close gate fail-closes (faithful)

    # The completion gate is self-contained: `completion_precheck` assembles the verifier's
    # graph-aware, fenced ticket context (epics verify across their descendants) and the verify
    # step consumes it — so the workflow path no longer loses the descendant context bespoke
    # supplies. (`graph` here is informational; the precheck resolves graph by ticket type, the
    # same default the close gate always uses.)
    del graph
    doc = _gate_doc("completion-verification", repo_root)
    res = _ex.run_workflow(
        doc,
        {"ticket_id": ticket_id},
        target_ticket=ticket_id,
        repo_root=repo_root,
        agent_runner=RunnerAgentStep(runner=runner_sel, repo_root=repo_root, config=cfg),
        recorder=MemoryRecorder(),
    )
    verdict = res.terminal_output
    if res.status != "succeeded" or not isinstance(verdict, dict) or "verdict" not in verdict:
        # The verifier failed mid-run — fail closed (never a silent PASS). Raise so the
        # close gate blocks, mirroring the bespoke path's raise-on-failed-run.
        from rebar.llm.errors import LLMError

        raise LLMError(
            f"completion verification workflow did not produce a verdict: "
            f"{res.error or 'LLM tier failed'}"
        )
    return verdict


__all__ = ["produce_plan_review_verdict", "produce_completion_verdict"]
