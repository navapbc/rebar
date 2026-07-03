"""Planned-trace capture — the engine-side proof of epic A's parity-validation mechanism.

The planned trace is the ordered set of intended execution events a run WOULD issue — each
LLM call's ``(prompt-id, intended model, call-mode, batched criteria)`` plus the deterministic
steps — captured OFFLINE (no live calls) so a diverse scenario corpus can be diffed cheaply
and in CI.

:class:`PlannedTraceRunner` wraps an :class:`~rebar.llm.workflow.executor.AgentStepRunner` and
records every call's planned shape before delegating to the inner runner (a ``FakeAgentRunner``
by default, so capture spends no tokens). Epic A (story A3) ships + proves it on a disposable
review skeleton.
"""

from __future__ import annotations

from typing import Any

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


def _execution_mode(prompt_id: str | None, repo_root: str | None) -> str | None:
    """The prompt's ``execution_mode`` (``single_turn`` | ``agentic``) — the call-mode the
    parity check compares. Resolved offline from the prompt front-matter; ``None``/``unknown``
    when the prompt can't be loaded (never raises — trace capture must not fail a run)."""
    if not prompt_id:
        return None
    try:
        from rebar.llm.prompting.prompts import get_prompt

        return getattr(get_prompt(prompt_id, repo_root=repo_root), "execution_mode", None)
    except Exception:  # noqa: BLE001 — best-effort call-mode lookup; trace capture never fails the run
        return "unknown"
