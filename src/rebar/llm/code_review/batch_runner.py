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

from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from rebar.llm.errors import LLMError
from rebar.llm.model_classes import resolve_model_string
from rebar.llm.workflow.runners import BatchRunner, BatchRunRequest, BatchRunResult

_FINDINGS_SCHEMA = "code_review_findings"
_ROUND_A_STEP_ID = "round_a"


class CodeReviewBatchRunner(BatchRunner):
    """Run each INCLUDED overlay's own prompt as a structured finder over the diff context."""

    def __init__(
        self,
        context: str = "",
        context_overrides: dict[str, str] | None = None,
        project_criteria: Sequence[Mapping[str, str]] = (),
        project_criteria_root: str | None = None,
    ) -> None:
        self._context = context
        # Per-overlay ticket_context overrides keyed by prompt_id. When an overlay's prompt_id is
        # present, that string is injected as ITS ticket_context instead of the shared diff
        # ``_context``; every other overlay keeps the shared diff (base + others stay
        # ticket-blind). Additive + default-None: absent ⇒ the single-context behaviour is
        # unchanged. produce_code_review_verdict populates {"code-review-scope-intent": <union
        # ticket scope>} ONLY when the commit's rebar-ticket trailers resolve >=1 ticket.
        self._context_overrides = context_overrides or {}
        # Project criteria are assembled by the gate dispatcher from the repository's active
        # code-review registry. They are additive because the static workflow schema owns the
        # built-in overlay entries; project-owned entries have no YAML slot.
        self._project_criteria = tuple(project_criteria)
        # The repo root the ``project_criteria`` were DISCOVERED from. Criterion discovery and
        # prompt resolution use deliberately different root rules (discovery falls back to the
        # ambient project root; ``get_prompt`` does not), so the two can silently diverge — a
        # criterion activated from checkout A while its rubric is sought under request root B,
        # which surfaces as a bogus "unknown prompt". Recording the discovery root lets
        # ``_validated_project_criteria`` assert the two agree instead of mis-reporting.
        self._project_criteria_root = project_criteria_root

    def run(self, req: BatchRunRequest, agent_runner: Any = None) -> BatchRunResult:
        from rebar.llm.workflow.executor import StepContext

        # The entry rung is a MODEL CLASS name (`trivial`/`standard`/`frontier`) resolved at run
        # time against the configured class table, so the overlays follow the RUN's provider
        # instead of the ladder's historical bare Anthropic ids (task 7761). A non-class string
        # resolves to itself; an EMPTY ladder still means "no per-step model" (None).
        model = (
            resolve_model_string(req.model_ladder[0], req.repo_root) if req.model_ladder else None
        )
        plan: dict[str, Any] = {
            "finder": req.finder,
            "ran": [],
            "criteria_count": 0,
        }
        findings: list[Any] = []
        active_criteria: list[Mapping[str, Any]] = list(req.criteria)
        # Project criteria run in the stable, deterministic Round-A fan-in only. They must not
        # enter Round B, whose membership is intentionally controlled solely by escalation.
        if req.step_id == _ROUND_A_STEP_ID:
            active_criteria.extend(self._validated_project_criteria(req.repo_root))
        for crit in active_criteria:
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
                    # Model-max output budget (bug 30a2): every Round-A/Round-B overlay
                    # finder (incl. project criteria) rides at the resolved model's
                    # maximum output capacity — the shared review-kernel rule, honored
                    # by RunnerAgentStep's `output_budget` input.
                    "output_budget": "model_max",
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
                    logical_id = crit.get("criterion_id")
                    if isinstance(logical_id, str) and logical_id:
                        tags = f.get("criteria")
                        if isinstance(tags, list):
                            if logical_id not in tags:
                                tags.append(logical_id)
                        else:
                            f["criteria"] = [logical_id]
            findings.extend(emitted)
            plan["ran"].append(prompt_id)
        plan["criteria_count"] = len(plan["ran"])
        return BatchRunResult(
            outputs={"findings": findings, "criteria_count": len(plan["ran"]), "batch_plan": plan}
        )

    def _validated_project_criteria(self, repo_root: str | None) -> tuple[Mapping[str, str], ...]:
        """Return usable project entries, failing with a located error on bad prompts.

        Before resolving each real entry, assert that the root the criteria were DISCOVERED
        from is the root we are about to RESOLVE their rubrics against. A divergence is a
        wiring bug, not project-prompt content, so it propagates as
        :class:`RepoRootMismatchError` rather than being wrapped in an ``LLMError`` that
        would blame the (perfectly present) rubric."""
        from rebar.llm.criteria import check_repo_root_agreement
        from rebar.llm.prompting.prompts import PromptError, get_prompt

        validated: list[Mapping[str, str]] = []
        for entry in self._project_criteria:
            logical_id = entry.get("criterion_id")
            prompt_id = entry.get("prompt")
            if not isinstance(logical_id, str) or not isinstance(prompt_id, str):
                continue
            check_repo_root_agreement(
                self._project_criteria_root,
                repo_root,
                where=f"code-review project criterion {logical_id!r}",
            )
            expected = Path(".rebar") / "prompts" / f"{prompt_id}.md"
            try:
                prompt = get_prompt(prompt_id, repo_root=repo_root)
            except PromptError as exc:
                raise LLMError(
                    f"project criterion {logical_id!r} requires a valid prompt at {expected}: {exc}"
                ) from exc
            if prompt.outputs != _FINDINGS_SCHEMA:
                raise LLMError(
                    f"project criterion {logical_id!r} requires a valid prompt at {expected}: "
                    f"expected outputs {_FINDINGS_SCHEMA!r}, got {prompt.outputs!r}"
                )
            validated.append(entry)
        return tuple(validated)
