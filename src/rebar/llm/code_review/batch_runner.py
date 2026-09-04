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

from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from rebar.llm.errors import LLMError, LLMUnavailableError
from rebar.llm.model_classes import resolve_model_string
from rebar.llm.review_kernel.discovery import (
    DiscoveryStagePlan,
    DiscoveryUnitPlan,
    LocalOperationExhausted,
    SystemicDiscoveryError,
    Usage,
    execute_stage,
    unit_trace,
)
from rebar.llm.workflow.runners import BatchRunner, BatchRunRequest, BatchRunResult

_FINDINGS_SCHEMA = "code_review_findings"
_ROUND_A_STEP_ID = "round_a"


def build_code_review_tf_provider(
    *, repo_root: str, changed_files: object, usage_sink: dict[str, Any]
) -> Callable[[Any], tuple[list, Callable[[], None]] | None]:
    from rebar.llm.code_review import workflow_ops
    from rebar.llm.plan_review import terraform_seam

    def _selected_changed() -> list[str]:
        if not isinstance(changed_files, (list, tuple, set, frozenset)):
            return []
        return [
            path
            for path in changed_files
            if isinstance(path, str) and path.endswith(workflow_ops._TF_SCOPE_SUFFIXES)
        ]

    def provider(ctx: Any) -> tuple[list, Callable[[], None]] | None:
        step = getattr(ctx, "step", None)
        prompt = (step if isinstance(step, dict) else {}).get("prompt")
        selected: list[str] = []
        if prompt == "code-review-iac" and workflow_ops.changed_terraform_scope(changed_files):
            selected = _selected_changed()
        elif prompt == "code-review-verify" and workflow_ops.changed_terraform_scope(changed_files):
            inputs = getattr(ctx, "inputs", None)
            findings = (inputs if isinstance(inputs, dict) else {}).get("findings")
            selected = workflow_ops._selected_terraform_paths_from_iac_findings(findings)
        if not selected:
            return None
        return terraform_seam.build_tool_provider(
            repo_root=repo_root, selected=selected, usage_sink=usage_sink, force=True
        )(ctx)

    return provider


class CodeReviewBatchRunner(BatchRunner):
    """Run each INCLUDED overlay's own prompt as a structured finder over the diff context.

    RP-06 S3: the criterion fan-in executes THROUGH the shared discovery kernel
    (:func:`rebar.llm.review_kernel.discovery.execute_stage`) — each included criterion is one
    ``DiscoveryUnitPlan``, dispatched with per-unit failure isolation and explicit-budget
    shedding — while the observable outputs contract stays byte-identical for the no-delta case
    (same ``findings``/``criteria_count``/``batch_plan``/``_usage`` shape). The kernel's SAFE
    per-unit trace is recorded on ``batch_plan["discovery_trace"]`` (internal journal), never in
    the review response or on any finding."""

    #: The per-unit budget estimate the kernel uses when shedding units under an explicit
    #: ``usd_budget``. Uniform across criteria (one criterion = one finder call).
    CRITERION_COST_ESTIMATE: float = 1.0

    def __init__(
        self,
        context: str = "",
        context_overrides: dict[str, str] | None = None,
        project_criteria: Sequence[Mapping[str, str]] = (),
        project_criteria_root: str | None = None,
        changed_files: Sequence[str] = (),
    ) -> None:
        self._context = context
        # The review's changed-file set. When non-empty it gates PROJECT criteria through their
        # ``applies_to`` globs (a non-applicable project criterion is dropped BEFORE any model
        # call); empty preserves the pre-cutover behaviour (no applicability filtering).
        self._changed_files = tuple(changed_files)
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
        from rebar.llm.plan_review import sizing
        from rebar.llm.plan_review.pass1 import aggregate_usage

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
            "discovery_trace": [],
        }
        findings: list[Any] = []
        # One per-CALL usage record per SUCCESSFUL overlay dispatch (task 514d). Each dispatch is
        # a single AGENT call covering exactly one criterion, so attribution is whole — no
        # equal-split approximation applies here, unlike plan-review's multi-criterion chunks.
        call_records: list[dict[str, Any]] = []
        active_criteria = self._included_criteria(req)
        by_unit = {self._unit_id(crit): crit for crit in active_criteria}
        stash: dict[str, dict[str, Any]] = {}

        def run_unit(unit: DiscoveryUnitPlan) -> tuple[dict[str, Any], Usage]:
            return self._run_criterion(
                by_unit[unit.unit_id], unit.unit_id, req, model, agent_runner, stash
            )

        units = tuple(self._unit_plan(crit, model) for crit in active_criteria)
        stage = DiscoveryStagePlan(
            units=units, budget=req.usd_budget, material="", code_ref="", topology_digest=""
        )
        result = execute_stage(stage, run_unit, store=None)
        units_by_id = {u.unit_id: u for u in units}
        for outcome in result.outcomes:
            plan["discovery_trace"].append(
                unit_trace(outcome, unit_plan=units_by_id[outcome.unit_id])
            )
            if outcome.kind not in ("success", "resumed"):
                continue
            crit = by_unit[outcome.unit_id]
            prompt_id = str(crit.get("prompt"))
            out = stash.get(outcome.unit_id) or {}
            logical_id = crit.get("criterion_id")
            call_records.append(
                sizing.usage_record(
                    [str(logical_id or prompt_id)],
                    out.get("_usage") if isinstance(out.get("_usage"), dict) else None,
                )
            )
            findings.extend(
                self._tag_findings(out.get("findings", []) or [], prompt_id, logical_id)
            )
            plan["ran"].append(prompt_id)
        plan["criteria_count"] = len(plan["ran"])
        # Pass-1 finder token usage (task 514d). The full aggregate — raw per-call records, the
        # derived per-criterion map, and the totals — rides on `batch_plan`, the same place
        # plan-review keeps it (`coverage["usage"]`, story d52a), so the two paths share one
        # helper and one payload shape.
        usage = aggregate_usage(call_records)
        plan["usage"] = usage
        return BatchRunResult(
            outputs={
                "findings": findings,
                "criteria_count": len(plan["ran"]),
                "batch_plan": plan,
                # The step-output `_usage` is the FLAT totals, not the nested aggregate:
                # `finalize._attach_code_review_metrics` folds `_usage` by reading the token
                # fields at the TOP level (the shape `runner._extract_usage` attaches to an
                # agent step), so handing it the nested aggregate would contribute zero and
                # leave the CloudWatch totals under-reporting exactly as before. Plan-review's
                # consumer (`_attach_plan_review_metrics`) folds the nested form instead — the
                # divergence is in the two CONSUMERS, so each producer emits what its own
                # consumer reads while both derive it from `aggregate_usage`.
                "_usage": dict(usage["totals"]),
            }
        )

    def _included_criteria(self, req: BatchRunRequest) -> list[Mapping[str, Any]]:
        """The criteria included in this fan-in: the request's built-in overlay entries, plus —
        for Round A only — the validated, applicability-filtered project criteria. Only entries
        carrying a ``prompt`` are kept (a discovery unit needs a prompt to dispatch)."""
        active: list[Mapping[str, Any]] = [c for c in req.criteria if c.get("prompt")]
        # Project criteria run in the stable, deterministic Round-A fan-in only. They must not
        # enter Round B, whose membership is intentionally controlled solely by escalation.
        if req.step_id == _ROUND_A_STEP_ID:
            active.extend(self._validated_project_criteria(req.repo_root))
        return active

    @staticmethod
    def _unit_id(crit: Mapping[str, Any]) -> str:
        """The kernel unit id for a criterion: its logical ``criterion_id`` when present, else
        its ``prompt`` (built-in overlays carry no ``criterion_id``)."""
        logical = crit.get("criterion_id")
        return str(logical) if isinstance(logical, str) and logical else str(crit.get("prompt"))

    def _unit_plan(self, crit: Mapping[str, Any], model: str | None) -> DiscoveryUnitPlan:
        """One frozen discovery-unit plan for a criterion — independent (no dependencies) and
        non-blocking, priced at the uniform :data:`CRITERION_COST_ESTIMATE`."""
        return DiscoveryUnitPlan(
            unit_id=self._unit_id(crit),
            prompt_id=str(crit.get("prompt")),
            contract_id=_FINDINGS_SCHEMA,
            model=model or "",
            mode="structured",
            context_digest="",
            policy_digest="",
            blocking=False,
            budget_estimate=self.CRITERION_COST_ESTIMATE,
        )

    def _run_criterion(
        self,
        crit: Mapping[str, Any],
        unit_id: str,
        req: BatchRunRequest,
        model: str | None,
        agent_runner: Any,
        stash: dict[str, dict[str, Any]],
    ) -> tuple[dict[str, Any], Usage]:
        """The per-unit model-call boundary: dispatch ONE criterion's finder and translate its
        failures into the kernel's typed signals — a provider outage is SYSTEMIC (aborts the
        stage), any other LLM error is a LOCAL exhaustion (isolated to this unit)."""
        ctx = self._step_context(crit, req, model)
        try:
            out = agent_runner.run(ctx).outputs or {}
        except LLMUnavailableError as exc:
            raise SystemicDiscoveryError(str(exc)) from exc
        except LLMError as exc:
            raise LocalOperationExhausted(str(exc)) from exc
        stash[unit_id] = out
        usage_raw = out.get("_usage")
        usage = usage_raw if isinstance(usage_raw, dict) else {}
        return out, Usage(
            input_tokens=int(usage.get("input_tokens", 0) or 0),
            output_tokens=int(usage.get("output_tokens", 0) or 0),
            requests=1,
        )

    def _step_context(self, crit: Mapping[str, Any], req: BatchRunRequest, model: str | None):
        from rebar.llm.workflow.executor import StepContext

        prompt_id = str(crit.get("prompt"))
        step: dict[str, Any] = {
            "prompt": prompt_id,
            "mode": "structured",
            "output_schema": _FINDINGS_SCHEMA,
        }
        if model:
            step["model"] = model
        return StepContext(
            run_id=req.run_id,
            step_id=f"{req.step_id}:{prompt_id}",
            kind="agent",
            step=step,
            inputs={
                "ticket_context": self._context_overrides.get(prompt_id, self._context),
                "ticket_id": "(code review)",
                # Model-max output budget (bug 30a2): every Round-A/Round-B overlay finder
                # (incl. project criteria) rides at the resolved model's maximum output
                # capacity — the shared review-kernel rule, honored by RunnerAgentStep's
                # `output_budget` input.
                "output_budget": "model_max",
            },
            workflow=req.workflow,
            target_ticket=req.target_ticket,
            repo_root=req.repo_root,
        )

    @staticmethod
    def _tag_findings(emitted: list[Any], prompt_id: str, logical_id: Any) -> list[Any]:
        """Provenance-tag each finding with the overlay that emitted it (so Pass-2 can re-ground
        it and merge_findings can record agreement across reviewers) and, for a project
        criterion, its logical id (at most once)."""
        for f in emitted:
            if isinstance(f, dict):
                f.setdefault("reviewer_id", prompt_id)
                if isinstance(logical_id, str) and logical_id:
                    tags = f.get("criteria")
                    if isinstance(tags, list):
                        if logical_id not in tags:
                            tags.append(logical_id)
                    else:
                        f["criteria"] = [logical_id]
        return emitted

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
            # Applicability gate (RP-06 S3): with a non-empty changed-file set, a project
            # criterion whose ``applies_to`` globs do not admit the review is dropped BEFORE any
            # validation or model call (zero cost). An empty changed-file set preserves the
            # pre-cutover behaviour (no filtering).
            if self._changed_files and not self._project_applies(logical_id, repo_root):
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

    def _project_applies(self, criterion_id: str, repo_root: str | None) -> bool:
        """Whether a project criterion's ``applies_to`` admits this review's changed files."""
        from rebar.llm.code_review import registry

        return registry.project_criterion_applies(criterion_id, self._changed_files, repo_root)
