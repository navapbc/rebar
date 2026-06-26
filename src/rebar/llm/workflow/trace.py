"""Planned-trace capture — the engine-side proof of epic B's parity-validation mechanism.

Epic B validates the plan-review migration by **planned-trace parity** (NOT result parity:
the review is LLM-dependent and variable). The planned trace is the ordered set of intended
execution events a run WOULD issue — each LLM call's ``(prompt-id, intended model, call-mode,
batched criteria)`` plus the deterministic steps — captured OFFLINE (no live calls) so a
diverse scenario corpus can be diffed cheaply and in CI.

:class:`PlannedTraceRunner` wraps an :class:`~rebar.llm.workflow.executor.AgentStepRunner` and
records every call's planned shape before delegating to the inner runner (a ``FakeAgentRunner``
by default, so capture spends no tokens). Epic A (story A3) ships + proves it on a disposable
skeleton; epic B runs BOTH the bespoke gate and the workflow against it and diffs the traces.
"""

from __future__ import annotations

import re
from typing import Any

from rebar.llm.runner import FakeRunner

from .executor import AgentStepRunner, FakeAgentRunner, StepContext, StepResult


class PlannedTraceRunner(AgentStepRunner):
    """Record each LLM call's PLANNED shape, then delegate to the inner runner (offline).

    The recorded ``trace`` is a list of ``{prompt, model, mode, call_mode, criteria}`` dicts in
    call order: ``prompt`` is the prompt-library id, ``model`` the INTENDED model override (or
    ``None`` for the default — NOT the runner's echoed model, which a fake leaves blank),
    ``mode`` the step output mode, ``call_mode`` the prompt's ``execution_mode``
    (``single_turn`` | ``agentic`` — i.e. 1-shot vs agent), and ``criteria`` the prompt-library
    ids batched into THIS call (set by a ``batch`` step's runner; ``[]`` for a plain agent step).
    """

    def __init__(self, inner: AgentStepRunner | None = None) -> None:
        self.inner = inner if inner is not None else FakeAgentRunner()
        self.trace: list[dict[str, Any]] = []

    def run(self, ctx: StepContext) -> StepResult:
        prompt_id = ctx.step.get("prompt")
        self.trace.append(
            {
                "prompt": prompt_id,
                "model": ctx.step.get("model"),  # intended override (None = default), not echoed
                "mode": ctx.step.get("mode", "findings"),
                "call_mode": _execution_mode(prompt_id, ctx.repo_root),
                "criteria": list(ctx.inputs.get("criteria") or []),
            }
        )
        return self.inner.run(ctx)


# ── the rebar.llm.Runner-seam tracer (plan-review's SHARED capture point) ────────
# Plan-review's Pass-1 finders (and, in the BESPOKE path, the Pass-2 verifier + Pass-4
# coach) drive a ``rebar.llm.Runner`` directly — NOT the engine's ``AgentStepRunner``.
# That is the seam BOTH plan-review paths share (the bespoke ``orchestrator.run_review``
# AND the workflow's ``ProductionBatchRunner``-driven finder batch), so a tracing
# ``rebar.llm.Runner`` is the capture point that makes the planned trace comparable
# (see ``docs/design/batch-runner-seam.md`` — the "two capture points" decision: the
# finders are traced here, one level below the engine's generic ``PlannedTraceRunner``,
# which only sees the verify/coach PROMPT steps).

# reviewer-id (RunRequest.reviewers[0]) → the planned-trace ROLE at this seam.
_REVIEWER_ROLE = {
    "plan-reviewer": "finder",
    "plan-container": "container",
    "plan-isf": "isf",
    "plan-isf-summarizer": "isf_summarize",
    "plan-verifier": "verify",
    "plan-coach": "coach",
}
# The criterion ids of a call, parsed from the STABLE instruction format passes.py
# emits (the RunRequest carries no explicit criterion field; the ids live in the
# instructions). Both plan-review paths render these identically, so parsing them the
# SAME way for both makes the per-criterion finder events directly comparable.
_FINDER_IDS_RE = re.compile(r"ids:\s*([^)\n]*)\)")
_CRITERION_RE = re.compile(r"##\s*Criterion\s+(\S+)")
_FINDING_INDEX_RE = re.compile(r"finding index (\d+)")


def _criteria_for(role: str, instructions: str | None) -> tuple[str, ...]:
    """The sorted criterion ids this call covers (parsed from the instructions)."""
    text = instructions or ""
    if role == "finder":
        m = _FINDER_IDS_RE.search(text)
        ids = [s.strip() for s in (m.group(1).split(",") if m else []) if s.strip()]
        return tuple(sorted(ids))
    if role == "container":
        m = _CRITERION_RE.search(text)
        return (m.group(1),) if m else ()
    if role == "isf":
        return ("ISF",)
    return ()


def _canned_verification(index: int) -> dict[str, Any]:
    """A high-validity, medium-impact verification (so the finding SURVIVES Pass-3 as a
    surfaced advisory — which keeps the Pass-4 coach on BOTH paths, matching shapes)."""
    return {
        "index": index,
        "severity_attributes": {
            "prod_impact": "medium",
            "debt_impact": "medium",
            "blast_radius": "module",
            "likelihood": "medium",
            "reversibility": "moderate",
        },
        "binary": {
            "cited_reference_accurate": "na",
            "is_verifiable": "yes",
            "evidence_entails_finding": "yes",
            "path_reachable": "yes",
            "impact_follows_necessarily": "yes",
            "no_viable_alternative_explanation": "yes",
            "no_existing_mitigation": "yes",
            "severity_claim_justified": "yes",
        },
    }


class TracingFakeRunner(FakeRunner):
    """An offline ``rebar.llm.Runner`` that RECORDS each call's planned shape and returns
    a deterministic, role-appropriate canned payload — no model, no network.

    Each call appends one planned-trace event ``{role, criteria, call_mode, model}`` to
    ``trace`` (in call order; the Pass-1 finder pool means finder events arrive in a
    concurrency-dependent order — sort by the event tuple to normalize):

    * ``role`` — derived from the request's reviewer id (finder / container / isf /
      verify / coach);
    * ``criteria`` — the sorted criterion ids in THIS call (parsed from the instructions);
    * ``call_mode`` — ``agent`` | ``1-shot`` (from the request's ``execution_mode``);
    * ``model`` — the INTENDED / SELECTED model (``req.config.model``, what the path
      CHOSE — NOT the fake's echoed ``model=None``).

    The returned payload is shaped to drive the four-pass pipeline to completion: the
    finder emits one finding per requested criterion, the verifier one (surviving)
    verification per listed finding index, and the coach one move pick. So a single
    instance, injected as the finder runner (workflow) or as the whole-pipeline runner
    (bespoke ``run_review``), captures + drives the offline run."""

    name = "fake"

    def __init__(self) -> None:
        super().__init__()
        self.trace: list[dict[str, Any]] = []

    def run(self, req) -> dict:  # type: ignore[override]
        from rebar.llm import findings as _findings

        role = _REVIEWER_ROLE.get(req.reviewers[0] if req.reviewers else "", "unknown")
        self.trace.append(
            {
                "role": role,
                "criteria": _criteria_for(role, req.instructions),
                "call_mode": "agent" if req.execution_mode == "agentic" else "1-shot",
                "model": req.config.model,  # INTENDED/selected model (not the echoed None)
            }
        )
        if req.mode == "text":  # the ISF oversize summarizer (single text call)
            return {
                "text": "[traced summary]",
                "runner": self.name,
                "model": None,
                "trace_id": None,
            }
        if req.mode != "structured":
            return _findings.finalize_findings(
                [],
                runner=self.name,
                model=None,
                trace_id=None,
                target=req.target,
                reviewers=req.reviewers,
                repo_path=req.config.repo_path,
            )
        schema = req.output_schema
        if schema == "plan_review_findings":
            ids = _criteria_for(role, req.instructions)
            payload: dict[str, Any] = {
                "analysis": "",
                "findings": [
                    {
                        "finding": f"[traced] finding for {cid}",
                        "criteria": [cid],
                        "evidence": [],
                        "scenarios": [],
                        "impact": "traced impact",
                    }
                    for cid in ids
                ],
            }
        elif schema == "plan_review_verification":
            idxs = [int(x) for x in _FINDING_INDEX_RE.findall(req.instructions or "")]
            payload = {"verifications": [_canned_verification(i) for i in idxs]}
        elif schema == "plan_review_coach":
            payload = {
                "notes": [{"move_id": "1", "subject": "the planned design", "finding_refs": []}]
            }
        else:
            payload = {}
        payload = _findings.validate_structured(dict(payload), schema)
        return {**payload, "runner": self.name, "model": None, "trace_id": None}


def _execution_mode(prompt_id: str | None, repo_root: str | None) -> str | None:
    """The prompt's ``execution_mode`` (``single_turn`` | ``agentic``) — the call-mode the
    parity check compares. Resolved offline from the prompt front-matter; ``None``/``unknown``
    when the prompt can't be loaded (never raises — trace capture must not fail a run)."""
    if not prompt_id:
        return None
    try:
        from rebar.llm.prompts import get_prompt

        return getattr(get_prompt(prompt_id, repo_root=repo_root), "execution_mode", None)
    except Exception:  # noqa: BLE001 — best-effort call-mode lookup; trace capture never fails the run
        return "unknown"
