"""The thin linear workflow executor (WS-C2).

Deliberately minimal. This runs a validated workflow's steps in
``graphlib.static_order`` (topological) order, threading each step's named outputs
forward so a downstream step can reference ``${{ steps.<id>.outputs.<name> }}``.
Scripted steps (WS-E) dispatch through a registry; agentic steps (WS-D) dispatch
through an injected runner. Both are SEAMS so this module owns control flow only,
not step internals.

**Thin on purpose (the Burr tripwire).** A single in-process, synchronous, linear
pass — NO ``asyncio`` / ``concurrent.futures`` / ``threading`` / ``multiprocessing``
/ retry libraries. ``tests/unit/workflow/test_executor_tripwire.py`` reads THIS
file and fails if any of those is imported, so the executor cannot silently grow a
scheduler. The run state is modeled as an immutable, copy-on-write
:class:`RunState` (a Burr-style ``State``) so that adopting Burr later is a swap,
not a rewrite.

Burr-adoption trigger list (adopt the framework only when one is TRUE — until then
this hand-rolled executor is correct and cheaper):
  1. Steps need durable PAUSE/RESUME across processes (human-in-the-loop holds that
     outlive the run), beyond our crash-recovery replay.
  2. Non-linear control flow lands: data-dependent branching/looping/fan-out the
     static DAG can't express.
  3. Parallel step execution becomes a hard requirement (concurrent independent
     steps), making a single linear pass the bottleneck.
  4. We need Burr's telemetry/UI as a product surface rather than our event log.
None hold today, so the tripwire stays armed.

Persistence (WORKFLOW_RUN/WORKFLOW_STEP events) goes through a :class:`RunRecorder`
seam; the in-memory default keeps this module testable, and the event-backed
recorder with marker-after-effect idempotency + determinism capture is WS-C3.
"""

from __future__ import annotations

import graphlib
import os
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from typing import Any

from rebar.llm.errors import WorkflowError, WorkflowValidationError

from .lint import lint_document
from .schema import step_kind, validate_document

# Reuse the linter's expression grammar so the resolver and the static checker can
# never disagree about what an expression is.
_EXPR_RE = re.compile(r"\$\{\{(.*?)\}\}", re.DOTALL)
_ENV_RE = re.compile(r"\$\{env:([A-Za-z_][A-Za-z0-9_]*)\}")
_INPUT_RE = re.compile(r"^inputs\.([A-Za-z_][A-Za-z0-9_-]*)$")
_STEP_OUT_RE = re.compile(r"^steps\.([A-Za-z_][A-Za-z0-9_-]*)\.outputs\.([A-Za-z_][A-Za-z0-9_-]*)$")
_SECRET_RE = re.compile(r"^secrets\.([A-Za-z_][A-Za-z0-9_]*)$")


# ── Step interfaces (the WS-E / WS-D seams) ──────────────────────────────────


@dataclass(frozen=True)
class StepContext:
    """Everything a step handler needs, with nothing it shouldn't have.

    ``inputs`` is the step's ``with:`` block AFTER expression substitution (so a
    handler never sees a raw ``${{ }}``). ``run_id``/``step_id`` is the idempotency
    token handed to non-idempotent downstream APIs (WS-C3).
    """

    run_id: str
    step_id: str
    kind: str
    step: Mapping[str, Any]
    inputs: Mapping[str, Any]
    workflow: Mapping[str, Any]
    target_ticket: str | None = None
    repo_root: str | None = None


@dataclass(frozen=True)
class StepResult:
    """A step's outcome: named outputs (wired forward) + a status."""

    outputs: dict[str, Any] = field(default_factory=dict)
    status: str = "succeeded"  # succeeded | failed | skipped
    error: str | None = None


# A scripted step is a pure-ish function of its context (WS-E registers these).
ScriptedStep = Callable[[StepContext], "StepResult | dict[str, Any]"]

# The registry scripted steps register into (WS-E1 owns the framework + built-ins).
# Empty here; the executor looks a step's `uses` up at dispatch time, so WS-E can
# populate it without touching this file.
STEP_REGISTRY: dict[str, ScriptedStep] = {}


def register_step(name: str) -> Callable[[ScriptedStep], ScriptedStep]:
    """Decorator: register a scripted step under ``name`` (used by WS-E)."""

    def deco(fn: ScriptedStep) -> ScriptedStep:
        STEP_REGISTRY[name] = fn
        return fn

    return deco


class AgentStepRunner:
    """The agentic-step seam (WS-D supplies the real LangGraph-backed runner)."""

    def run(self, ctx: StepContext) -> StepResult:  # pragma: no cover - interface
        raise NotImplementedError


class FakeAgentRunner(AgentStepRunner):
    """A no-token agent runner: deterministic, offline. Used for ``--dry-run`` and
    tests until WS-D wires the real runner. Echoes a stable, schema-shaped stub so
    downstream wiring can be exercised without a model call."""

    def run(self, ctx: StepContext) -> StepResult:
        mode = ctx.step.get("mode", "findings")
        if mode == "findings":
            outputs = {"findings": [], "summary": f"[fake] {ctx.step_id}", "_fake": True}
        elif mode == "text":
            outputs = {"text": f"[fake output for {ctx.step_id}]", "_fake": True}
        else:
            outputs = {"result": {}, "_fake": True}
        return StepResult(outputs=outputs, status="succeeded")


# ── Run recorder seam (WS-C3 supplies the event-backed, idempotent one) ───────


class RunRecorder:
    """Persistence seam for run-state. The default is in-memory; WS-C3 adds the
    WORKFLOW_RUN/WORKFLOW_STEP event-backed recorder with marker-after-effect
    idempotency."""

    def run_started(self, record: dict[str, Any]) -> None: ...
    def run_finished(self, record: dict[str, Any]) -> None: ...
    def step_recorded(self, record: dict[str, Any]) -> None: ...

    def completed_step(self, run_id: str, step_id: str) -> dict[str, Any] | None:
        """Return a prior SUCCEEDED step record for idempotent skip, or None.

        The in-memory default never skips (no prior runs); WS-C3's event recorder
        returns the persisted marker so a resumed run does not re-run a committed
        effect.
        """
        return None


class MemoryRecorder(RunRecorder):
    """Collects run/step records in memory (default; keeps the executor testable)."""

    def __init__(self) -> None:
        self.runs: list[dict[str, Any]] = []
        self.steps: list[dict[str, Any]] = []

    def run_started(self, record: dict[str, Any]) -> None:
        self.runs.append(record)

    def run_finished(self, record: dict[str, Any]) -> None:
        self.runs.append(record)

    def step_recorded(self, record: dict[str, Any]) -> None:
        self.steps.append(record)


# ── Burr-style immutable run state ────────────────────────────────────────────


@dataclass(frozen=True)
class RunState:
    """Immutable, copy-on-write run state (pre-shaped toward a Burr ``State``).

    Holds the workflow inputs and each completed step's outputs. Updates return a
    NEW instance — no in-place mutation — so the execution history is a sequence of
    immutable states, exactly the shape Burr would manage.
    """

    inputs: Mapping[str, Any] = field(default_factory=dict)
    outputs: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    statuses: Mapping[str, str] = field(default_factory=dict)

    def with_step(self, step_id: str, result: StepResult) -> RunState:
        new_outputs = dict(self.outputs)
        new_outputs[step_id] = dict(result.outputs)
        new_statuses = dict(self.statuses)
        new_statuses[step_id] = result.status
        return replace(self, outputs=new_outputs, statuses=new_statuses)


@dataclass(frozen=True)
class RunResult:
    """The outcome of a whole workflow run."""

    run_id: str
    workflow_name: str
    status: str  # succeeded | failed
    outputs: Mapping[str, Mapping[str, Any]]
    terminal_step: str | None
    terminal_output: Mapping[str, Any] | None
    error: str | None = None
    steps: Mapping[str, str] = field(default_factory=dict)  # step_id -> status


# ── Expression resolution (named-output wiring) ───────────────────────────────


class ExpressionError(WorkflowError):
    """An expression could not be resolved at run time (a value the linter could
    not have known was missing — e.g. an upstream step produced no such output)."""


def _resolve_one(expr: str, state: RunState, secrets: Mapping[str, str]) -> Any:
    expr = expr.strip()
    m = _INPUT_RE.match(expr)
    if m:
        name = m.group(1)
        if name not in state.inputs:
            raise ExpressionError(f"input {name!r} is not set for this run")
        return state.inputs[name]
    m = _STEP_OUT_RE.match(expr)
    if m:
        step, out = m.group(1), m.group(2)
        if step not in state.outputs:
            raise ExpressionError(f"step {step!r} has not produced outputs yet")
        if out not in state.outputs[step]:
            raise ExpressionError(f"step {step!r} did not produce output {out!r}")
        return state.outputs[step][out]
    m = _SECRET_RE.match(expr)
    if m:
        name = m.group(1)
        if name not in secrets:
            raise ExpressionError(f"secret {name!r} is not available")
        return secrets[name]
    raise ExpressionError(f"unresolvable expression {expr!r}")


def resolve_value(value: Any, state: RunState, secrets: Mapping[str, str]) -> Any:
    """Substitute every ``${{ … }}`` / ``${env:VAR}`` in ``value`` (recursively).

    A string that is EXACTLY one expression resolves to the raw referenced value
    (which may be a list/dict — e.g. a findings array wired between steps); an
    expression embedded in surrounding text is stringified in place.
    """
    if isinstance(value, dict):
        return {k: resolve_value(v, state, secrets) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve_value(v, state, secrets) for v in value]
    if not isinstance(value, str):
        return value

    whole = _EXPR_RE.fullmatch(value)
    if whole:
        return _resolve_one(whole.group(1), state, secrets)

    def sub_expr(m: re.Match[str]) -> str:
        return str(_resolve_one(m.group(1), state, secrets))

    out = _EXPR_RE.sub(sub_expr, value)

    def sub_env(m: re.Match[str]) -> str:
        var = m.group(1)
        if var not in os.environ:
            raise ExpressionError(f"environment variable {var!r} is not set")
        return os.environ[var]

    return _ENV_RE.sub(sub_env, out)


# ── Ordering + execution ──────────────────────────────────────────────────────


def static_order(doc: Mapping[str, Any]) -> list[str]:
    """The deterministic topological execution order of a workflow's steps."""
    graph: dict[str, set[str]] = {}
    for step in doc.get("steps", []):
        sid = step["id"]
        graph[sid] = {n for n in (step.get("needs") or []) if isinstance(n, str)}
    return list(graphlib.TopologicalSorter(graph).static_order())


def _terminal_step(doc: Mapping[str, Any]) -> str | None:
    """The single sink step (nothing depends on it) — the run's result step."""
    ids = [s["id"] for s in doc.get("steps", [])]
    depended: set[str] = set()
    for s in doc.get("steps", []):
        depended.update(s.get("needs") or [])
    sinks = [sid for sid in ids if sid not in depended]
    return sinks[-1] if sinks else None


def _guard_passes(step: Mapping[str, Any], state: RunState, secrets: Mapping[str, str]) -> bool:
    guard = step.get("if")
    if not guard:
        return True
    try:
        val = resolve_value(guard, state, secrets)
    except ExpressionError:
        return False
    if isinstance(val, str):
        return val.strip().lower() not in ("", "false", "0", "no")
    return bool(val)


def run_workflow(
    doc: Mapping[str, Any],
    inputs: Mapping[str, Any] | None = None,
    *,
    run_id: str,
    target_ticket: str | None = None,
    repo_root: str | None = None,
    scripted_registry: Mapping[str, ScriptedStep] | None = None,
    agent_runner: AgentStepRunner | None = None,
    recorder: RunRecorder | None = None,
    secrets: Mapping[str, str] | None = None,
) -> RunResult:
    """Execute a validated workflow ``doc`` start to finish, synchronously.

    Validates + lints first (raises :class:`WorkflowValidationError` on any error),
    then runs each step in ``static_order``, substituting expressions, dispatching
    scripted/agent steps, and threading named outputs forward. A failed step stops
    the run (forward-only; WS-C3 adds idempotent resume). Returns a :class:`RunResult`
    whose terminal-step output is the run's result.
    """
    registry = STEP_REGISTRY if scripted_registry is None else scripted_registry
    runner = FakeAgentRunner() if agent_runner is None else agent_runner
    rec = MemoryRecorder() if recorder is None else recorder
    secrets = secrets or {}
    inputs = dict(inputs or {})

    errors = validate_document(doc) + [str(f) for f in lint_document(doc)]
    errors = [e for e in errors if "[warning]" not in e]
    if errors:
        raise WorkflowValidationError(errors, source=str(doc.get("name", "<workflow>")))

    name = doc.get("name", "<workflow>")
    by_id = {s["id"]: s for s in doc.get("steps", [])}
    terminal = _terminal_step(doc)

    rec.run_started(
        {"run_id": run_id, "workflow_name": name, "status": "running", "inputs": inputs}
    )

    state = RunState(inputs=inputs)
    run_status = "succeeded"
    run_error: str | None = None

    for step_id in static_order(doc):
        step = by_id[step_id]
        kind = step_kind(step)

        if not _guard_passes(step, state, secrets):
            result = StepResult(outputs={}, status="skipped")
            state = state.with_step(step_id, result)
            rec.step_recorded(_step_record(run_id, step_id, kind, result))
            continue

        # Idempotent skip (WS-C3 recorder returns a committed marker; the in-memory
        # default never does, so a fresh run executes every step).
        prior = rec.completed_step(run_id, step_id)
        if prior is not None and prior.get("status") == "succeeded":
            result = StepResult(outputs=prior.get("outputs", {}), status="succeeded")
            state = state.with_step(step_id, result)
            continue

        try:
            resolved = resolve_value(dict(step.get("with") or {}), state, secrets)
            ctx = StepContext(
                run_id=run_id,
                step_id=step_id,
                kind=kind,
                step=step,
                inputs=resolved,
                workflow=doc,
                target_ticket=target_ticket,
                repo_root=repo_root,
            )
            result = _dispatch(ctx, registry, runner)
        except Exception as exc:  # a step failure is data, not a crash
            result = StepResult(outputs={}, status="failed", error=str(exc))

        state = state.with_step(step_id, result)
        rec.step_recorded(_step_record(run_id, step_id, kind, result))

        if result.status == "failed":
            run_status = "failed"
            run_error = f"step {step_id!r} failed: {result.error}"
            break

    terminal_output = state.outputs.get(terminal) if terminal else None
    rec.run_finished(
        {
            "run_id": run_id,
            "workflow_name": name,
            "status": run_status,
            "error": run_error,
            "terminal_step": terminal,
        }
    )
    return RunResult(
        run_id=run_id,
        workflow_name=name,
        status=run_status,
        outputs=dict(state.outputs),
        terminal_step=terminal,
        terminal_output=terminal_output,
        error=run_error,
        steps=dict(state.statuses),
    )


def _dispatch(
    ctx: StepContext,
    registry: Mapping[str, ScriptedStep],
    runner: AgentStepRunner,
) -> StepResult:
    if ctx.kind == "agent":
        result = runner.run(ctx)
        return result if isinstance(result, StepResult) else StepResult(outputs=dict(result))
    name = ctx.step.get("uses")
    handler = registry.get(name)
    if handler is None:
        raise WorkflowError(f"unknown scripted step {name!r} (not in the step registry)")
    out = handler(ctx)
    if isinstance(out, StepResult):
        return out
    return StepResult(outputs=dict(out) if isinstance(out, dict) else {})


def _step_record(run_id: str, step_id: str, kind: str, result: StepResult) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "step_id": step_id,
        "kind": kind,
        "status": result.status,
        "outputs": dict(result.outputs),
        "error": result.error,
    }


__all__ = [
    "StepContext",
    "StepResult",
    "ScriptedStep",
    "STEP_REGISTRY",
    "register_step",
    "AgentStepRunner",
    "FakeAgentRunner",
    "RunRecorder",
    "MemoryRecorder",
    "RunState",
    "RunResult",
    "ExpressionError",
    "resolve_value",
    "static_order",
    "run_workflow",
]
