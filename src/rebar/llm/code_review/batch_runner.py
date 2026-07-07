"""The code-review batch runner (epic b744 / WS3).

The v3 ``batch`` construct gives the gate its MEMBERSHIP gating (each criterion's ``when:``
resolves to the included set, journaled as ``included``/``skipped``). But the two shipped
batch runners don't fit code review's overlay model: ``DefaultBatchRunner`` runs ONE finder
prompt over the criteria-as-data, and ``ProductionBatchRunner`` is plan-review-bound (it drives
``run_pass1`` over ``plan_review.registry``). Code review's overlays are DISTINCT standalone
finder prompts (``code-review-<overlay>.md``), so this runner runs EACH included criterion's own
prompt as a structured finder over the diff context, aggregating their ``findings``.

It is the code-review analog of ``ProductionBatchRunner``: constructed with the assembled diff
``context`` (a BatchRunner can't read step outputs, and code review reviews a diff, not a
ticket), it injects that context into each overlay's ``ticket_context``. WS4's dispatch
constructs it with the context it assembled; the offline test constructs it directly.
"""

from __future__ import annotations

from typing import Any

from rebar.llm.workflow.runners import BatchRunner, BatchRunRequest, BatchRunResult

_FINDINGS_SCHEMA = "code_review_findings"


class CodeReviewBatchRunner(BatchRunner):
    """Run each INCLUDED overlay's own prompt as a structured finder over the diff context."""

    def __init__(self, context: str = "", context_overrides: dict[str, str] | None = None) -> None:
        self._context = context
        # Per-overlay ticket_context overrides keyed by prompt_id. When an overlay's prompt_id is
        # present, that string is injected as ITS ticket_context instead of the shared diff
        # ``_context``; every other overlay keeps the shared diff (base + others stay
        # ticket-blind). Additive + default-None: absent ⇒ the single-context behaviour is
        # unchanged. produce_code_review_verdict populates {"code-review-scope-intent": <union
        # ticket scope>} ONLY when the commit's rebar-ticket trailers resolve >=1 ticket.
        self._context_overrides = context_overrides or {}

    def run(self, req: BatchRunRequest, agent_runner: Any) -> BatchRunResult:
        from rebar.llm.workflow.executor import StepContext

        model = req.model_ladder[0] if req.model_ladder else None
        plan: dict[str, Any] = {
            "finder": req.finder,
            "ran": [],
            "criteria_count": len(req.criteria),
        }
        findings: list[Any] = []
        for crit in req.criteria:
            prompt_id = crit.get("prompt")
            if not prompt_id:
                continue
            step: dict[str, Any] = {
                "prompt": prompt_id,
                "mode": "structured",
                "output_schema": _FINDINGS_SCHEMA,
            }
            if model:
                step["model"] = model
            ctx = StepContext(
                run_id=req.run_id,
                step_id=f"{req.step_id}:{prompt_id}",
                kind="agent",
                step=step,
                inputs={
                    "ticket_context": self._context_overrides.get(prompt_id, self._context),
                    "ticket_id": "(code review)",
                },
                workflow=req.workflow,
                target_ticket=req.target_ticket,
                repo_root=req.repo_root,
            )
            out = agent_runner.run(ctx).outputs or {}
            emitted = out.get("findings", []) or []
            # Provenance: tag each finding with the overlay that emitted it (so Pass-2 can
            # re-ground it and merge_findings can record agreement across reviewers).
            for f in emitted:
                if isinstance(f, dict):
                    f.setdefault("reviewer_id", prompt_id)
            findings.extend(emitted)
            plan["ran"].append(prompt_id)
        return BatchRunResult(
            outputs={"findings": findings, "criteria_count": len(req.criteria), "batch_plan": plan}
        )
