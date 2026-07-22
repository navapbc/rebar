"""The pluggable step-execution runner seams (extracted from ``executor.py``).

These are the SEAMS the thin executor dispatches through — it owns control flow only,
not step internals (see the ``executor`` module docstring). Two families live here:

* **Agent runners** (:class:`AgentStepRunner` + the offline :class:`FakeAgentRunner`) —
  the agentic-step seam the real pydantic_ai-backed runner plugs into.
* **Batch runners** (the v3 ``batch`` construct: :class:`BatchRunRequest` /
  :class:`BatchRunResult` / :class:`BatchRunner` + the reference
  :class:`DefaultBatchRunner`) — budgeted batch orchestration that packs the included
  criteria into cost-bounded batches, runs the finder once per batch via an agent
  runner, and JOURNALS an opaque plan the interpreter stores but never branches on.

Split out so ``executor.py`` stays under the module-size cap along an existing
call-graph seam (the runner abstractions are a cohesive cluster). ``executor``
re-exports these names, so ``from .executor import FakeAgentRunner`` still resolves.
:class:`StepContext` / :class:`StepResult` are constructed at runtime via an in-method
import to keep this module import-cycle-free (it never imports ``executor`` at module
load — same thin/no-scheduler discipline as the executor).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .executor import StepContext, StepResult


class AgentStepRunner:
    """The agentic-step seam (the real pydantic_ai-backed runner plugs in here)."""

    def run(self, ctx: StepContext) -> StepResult:  # pragma: no cover - interface
        raise NotImplementedError


class FakeAgentRunner(AgentStepRunner):
    """A no-token agent runner: deterministic, offline. Used for ``--dry-run`` and
    tests until WS-D wires the real runner. Echoes a stable, schema-shaped stub so
    downstream wiring can be exercised without a model call."""

    def run(self, ctx: StepContext) -> StepResult:
        from .executor import StepResult

        mode = ctx.step.get("mode", "findings")
        if mode == "findings":
            outputs = {"findings": [], "summary": f"[fake] {ctx.step_id}", "_fake": True}
        elif mode == "text":
            outputs = {"text": f"[fake output for {ctx.step_id}]", "_fake": True}
        else:
            outputs = {"result": {}, "_fake": True}
        return StepResult(outputs=outputs, status="succeeded")


# ── Batch-runner seam (the v3 `batch` construct) ──────────────────────────────


@dataclass(frozen=True)
class BatchRunRequest:
    """What a batch-runner needs. The interpreter resolves each criterion's ``when``
    BEFORE building this, so ``criteria`` is the INCLUDED set only — each entry a
    prompt-library-backed ``{prompt, with?}``. ``finder`` is the per-batch prompt id."""

    finder: str
    criteria: tuple[Mapping[str, Any], ...]
    usd_budget: float | None
    model_ladder: tuple[str, ...]
    workflow: Mapping[str, Any]
    target_ticket: str | None
    repo_root: str | None
    run_id: str
    step_id: str
    # Step-level inputs resolved by the interpreter.  Kept separate from each
    # criterion's optional ``with`` mapping so existing batch runners can ignore
    # it without changing their packing behaviour.
    with_inputs: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class BatchRunResult:
    """A batch-runner's result. ``outputs`` is the ``batch`` step's output dict — it
    carries the aggregated ``findings`` AND the journaled ``batch_plan`` (OPAQUE to the
    interpreter, which stores it but never branches on its internals)."""

    outputs: dict[str, Any]


class BatchRunner:
    """The batch-orchestration seam: pack the included criteria into cost-bounded
    batches, run the ``finder`` once per batch (via the agent runner), handle
    context-limit fallback / model-escalation / budget-shedding, and JOURNAL an opaque
    plan. The PRODUCTION runner (epic B, extracted from the gate's ``sizing.py``) plugs
    in here; epic A ships the reference :class:`DefaultBatchRunner`."""

    def run(
        self, req: BatchRunRequest, agent_runner: AgentStepRunner | None = None
    ) -> BatchRunResult:  # pragma: no cover - interface
        raise NotImplementedError


class DefaultBatchRunner(BatchRunner):
    """Reference batch-runner (epic A): proves the seam + journaling end-to-end.

    Real but minimal — it packs the included criteria into batches of at most
    ``max_batch_size`` and runs the finder once per batch; on a per-batch context-limit
    signal (the agent result carries a truthy ``_context_limit``) it SPLITS the batch and
    retries each half (one genuine fallback path). ``usd_budget`` / ``model_ladder`` /
    shedding are accepted and RECORDED in the plan but NOT enforced — the production
    runner (epic B) implements the adaptive cost-escalation/shed. Journals a
    ``batch_plan`` the interpreter stores opaquely."""

    def __init__(self, max_batch_size: int = 4) -> None:
        self.max_batch_size = max_batch_size

    def run(
        self, req: BatchRunRequest, agent_runner: AgentStepRunner | None = None
    ) -> BatchRunResult:
        model = req.model_ladder[0] if req.model_ladder else None
        plan: dict[str, Any] = {
            "finder": req.finder,
            "max_batch_size": self.max_batch_size,
            "usd_budget": req.usd_budget,
            "model_ladder": list(req.model_ladder),
            "enforced": False,  # reference runner does not enforce budget/escalation (epic B does)
            "batches": [],
            "shed": [],
        }
        findings: list[Any] = []
        for i in range(0, len(req.criteria), self.max_batch_size):
            self._run_one(
                req,
                agent_runner,
                list(req.criteria[i : i + self.max_batch_size]),
                model,
                plan,
                findings,
                0,
            )
        return BatchRunResult(
            outputs={"findings": findings, "criteria_count": len(req.criteria), "batch_plan": plan}
        )

    def _run_one(self, req, agent_runner, batch, model, plan, findings, depth) -> None:
        from .executor import StepContext

        ids = [c.get("prompt") for c in batch]
        ctx = StepContext(
            run_id=req.run_id,
            step_id=f"{req.step_id}:batch{len(plan['batches'])}",
            kind="agent",
            step={"prompt": req.finder, "mode": "findings", **({"model": model} if model else {})},
            inputs={"finder": req.finder, "criteria": ids},
            workflow=req.workflow,
            target_ticket=req.target_ticket,
            repo_root=req.repo_root,
        )
        out = agent_runner.run(ctx).outputs or {}
        if out.get("_context_limit") and len(batch) > 1 and depth < 8:
            # Fallback: a batch that hit the context limit is split and each half retried.
            mid = len(batch) // 2
            plan["batches"].append({"criteria": ids, "model": model, "outcome": "split"})
            self._run_one(req, agent_runner, batch[:mid], model, plan, findings, depth + 1)
            self._run_one(req, agent_runner, batch[mid:], model, plan, findings, depth + 1)
            return
        plan["batches"].append({"criteria": ids, "model": model, "outcome": "ran"})
        findings.extend(out.get("findings", []) or [])


__all__ = [
    "AgentStepRunner",
    "FakeAgentRunner",
    "BatchRunRequest",
    "BatchRunResult",
    "BatchRunner",
    "DefaultBatchRunner",
]
