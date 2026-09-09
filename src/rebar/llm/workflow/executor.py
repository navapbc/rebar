"""Synchronous topological workflow execution.

Scripted and agentic steps use injected seams while immutable :class:`RunState`
threads named outputs. A tripwire forbids scheduler and retry imports. Adopt Burr only for:
1. durable cross-process pause/resume;
2. data-dependent non-linear flow;
3. required parallel step execution; or
4. Burr telemetry/UI as a product surface.
The :class:`RunRecorder` seam supplies persistence, determinism, and idempotency.
"""

from __future__ import annotations

import graphlib
import os
import re
import time
import uuid as _uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from rebar.llm.errors import WorkflowError, WorkflowUnknownStepError, WorkflowValidationError

from .lint import lint_document
from .runners import (
    AgentStepRunner,
    BatchRunner,
    BatchRunRequest,
    BatchRunResult,
    DefaultBatchRunner,
    FakeAgentRunner,
)
from .schema import validate_document

# The step-kind CONTRACT model + the static compatibility check live in
# step_contracts.py; imported here because `register_step` populates STEP_CONTRACTS, and
# re-exported via __all__ so existing executor.StepContract / STEP_CONTRACTS /
# contract_for / shallow_contract_check references keep working.
from .step_contracts import (
    STEP_CONTRACTS,
    StepContract,
    contract_for,
    shallow_contract_check,
)

# Reuse the linter's expression grammar so the resolver and the static checker can
# never disagree about what an expression is.
_EXPR_RE = re.compile(r"\$\{\{(.*?)\}\}", re.DOTALL)
_ENV_RE = re.compile(r"\$\{env:([A-Za-z_][A-Za-z0-9_]*)\}")
_INPUT_RE = re.compile(r"^inputs\.([A-Za-z_][A-Za-z0-9_-]*)$")
_STEP_OUT_RE = re.compile(r"^steps\.([A-Za-z_][A-Za-z0-9_-]*)\.outputs\.([A-Za-z_][A-Za-z0-9_-]*)$")
_SECRET_RE = re.compile(r"^secrets\.([A-Za-z_][A-Za-z0-9_]*)$")


# ── Run identity + non-determinism capture (WS-C3) ───────────────────────────


def new_run_id() -> str:
    """Return a sortable, globally unique ``{ns-timestamp}-{uuid4hex}`` run id.

    It is generated once and persisted on every run and step event.
    """
    return f"{time.time_ns()}-{_uuid.uuid4().hex}"


def _capture_nondeterminism() -> dict[str, Any]:
    """Capture the engine clock, UUID, and seed once per step execution.

    Persisted values replay through ``ctx.captured``. A pre-marker crash may
    re-execute and recapture; effect safety remains the step's idempotency contract.
    """
    return {
        "now_ns": time.time_ns(),
        "uuid": _uuid.uuid4().hex,
        "seed": _uuid.uuid4().int & 0xFFFFFFFF,
    }


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
    # Full execution frame key. Side effects use ``(run_id, frame_key)`` so nested
    # iterations remain distinct and replay-stable.
    frame_key: str = ""
    # The immediate enclosing loop/map iteration index (None at the top frame). Carried
    # so a step can read its own iteration; the full path is in ``frame_key``.
    iteration: int | None = None
    # Persisted clock, UUID, and seed for this execution; status/result reads replay
    # these values instead of consulting live sources.
    captured: Mapping[str, Any] = field(default_factory=dict)


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


def register_step(
    name: str,
    *,
    input_schema: str | None = None,
    output_schema: str | None = None,
    description: str | None = None,
) -> Callable[[ScriptedStep], ScriptedStep]:
    """Register a scripted step and expose supplied contract metadata.

    The editor and reference linter consume the resulting :class:`StepContract`.
    """

    def deco(fn: ScriptedStep) -> ScriptedStep:
        STEP_REGISTRY[name] = fn
        if input_schema is not None or output_schema is not None or description is not None:
            STEP_CONTRACTS[name] = StepContract(
                input_schema=input_schema,
                output_schema=output_schema,
                description=description or "",
            )
        return fn

    return deco


# Runner seams live in runners.py, remain re-exported here, and are constructed per run.


# ── Recorder seam ─────────────────────────────────────────────────────────────
# Recorder classes live in recorder.py but remain re-exported for compatibility.
from .recorder import MemoryRecorder, RunRecorder, TicketEventRecorder  # noqa: E402

__all_recorders__ = ("RunRecorder", "MemoryRecorder", "TicketEventRecorder")


# ── Burr-style immutable run state ────────────────────────────────────────────


@dataclass(frozen=True)
class RunState:
    """Immutable workflow inputs and completed outputs; updates return a new state."""

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

    error_code = "invalid_input"


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
    """Recursively resolve step/input and environment expressions in ``value``.

    A whole-string expression preserves its raw type; embedded values become text.
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
        return os.environ[var]  # read-via: workflow-expression-env-control

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


# ── v2 worklist interpreter ──────────────────────────────────────────────────
# Recursive frame execution lives in interpreter and is imported lazily to avoid
# cycling back to this module's step interfaces. The Burr tripwire scans both files.


def _resolve_terminal_output(rc, steps: list, terminal_sid: str | None, prefix: str = "") -> Any:
    """Return the executed terminal leaf output, recursively following taken branches.

    Non-branch terminals retain their own output; nested results use full frame keys.
    """
    if not terminal_sid:
        return None
    step = next((s for s in steps if isinstance(s, dict) and s.get("id") == terminal_sid), None)
    fk = f"{prefix}{terminal_sid}"
    if step and "branch" in step:
        taken = (rc.outputs.get(fk) or {}).get("taken")
        arm = (step.get("branch") or {}).get(taken) if taken else None
        if isinstance(arm, list) and arm:
            # Arms are authored linearly, so the last step is the arm's terminal.
            arm_terminal = next(
                (s.get("id") for s in reversed(arm) if isinstance(s, dict) and s.get("id")), None
            )
            return _resolve_terminal_output(rc, arm, arm_terminal, f"{fk}@{taken}/")
    return rc.outputs.get(fk)


def run_workflow(
    doc: Mapping[str, Any],
    inputs: Mapping[str, Any] | None = None,
    *,
    run_id: str | None = None,
    target_ticket: str | None = None,
    repo_root: str | None = None,
    scripted_registry: Mapping[str, ScriptedStep] | None = None,
    agent_runner: AgentStepRunner | None = None,
    batch_runner: BatchRunner | None = None,
    recorder: RunRecorder | None = None,
    secrets: Mapping[str, str] | None = None,
) -> RunResult:
    """Validate, lint, and synchronously execute ``doc`` to its terminal output.

    Steps resolve expressions and thread named outputs; failure stops the run. A fresh
    id is used by default. ``target_ticket`` selects durable ticket recording when no
    recorder is supplied; otherwise state remains in memory.
    """
    run_id = run_id or new_run_id()
    registry = STEP_REGISTRY if scripted_registry is None else scripted_registry
    runner = FakeAgentRunner() if agent_runner is None else agent_runner
    batcher = DefaultBatchRunner() if batch_runner is None else batch_runner
    if recorder is None:
        recorder = (
            TicketEventRecorder(target_ticket, repo_root) if target_ticket else MemoryRecorder()
        )
    rec = recorder
    secrets = secrets or {}
    inputs = dict(inputs or {})
    # Fill only missing inputs from declared defaults; explicit falsy values still win.
    for _name, _spec in (doc.get("inputs") or {}).items():
        if _name not in inputs and isinstance(_spec, dict) and "default" in _spec:
            inputs[_name] = _spec["default"]

    # Block only on real errors: the informational "note:" line (degraded
    # jsonschema-absent path) and lint warnings never stop a run.
    doc_dict = dict(doc)
    schema_errors = [e for e in validate_document(doc_dict) if not e.startswith("note:")]
    lint_errors = [str(f) for f in lint_document(doc_dict) if f.severity != "warning"]
    errors = schema_errors + lint_errors
    if errors:
        raise WorkflowValidationError(errors, source=str(doc.get("name", "<workflow>")))

    name = doc.get("name", "<workflow>")
    terminal = _terminal_step(doc)

    rec.run_started(
        {"run_id": run_id, "workflow_name": name, "status": "running", "inputs": inputs}
    )

    # Walk v2 frames. Leaf-only workflows retain v1 bare step keys and compatible
    # markers/results. The lazy import avoids cycling through the step interfaces.
    from .interpreter import _execute_frame, _RunCtx

    rc = _RunCtx(
        run_id=run_id,
        doc=doc,
        registry=registry,
        runner=runner,
        rec=rec,
        secrets=secrets,
        inputs=inputs,
        target_ticket=target_ticket,
        repo_root=repo_root,
        batch_runner=batcher,
    )
    _execute_frame(rc, list(doc.get("steps", [])), ("",), {}, None)

    run_status = "failed" if rc.failed else "succeeded"
    run_error = rc.error
    # A terminal branch returns routing metadata, so follow its taken arm to the verdict;
    # plain terminals keep their own output.
    terminal_output = (
        _resolve_terminal_output(rc, list(doc.get("steps", [])), terminal) if terminal else None
    )
    rec.run_finished(
        {
            "run_id": run_id,
            "workflow_name": name,
            "status": run_status,
            "error": run_error,
            "terminal_step": terminal,
        }
    )
    # RunResult.outputs is the TOP frame, keyed by bare step id (v1-compatible);
    # nested-frame outputs live in the event log (and rc.outputs by path). steps
    # carries every executed frame_key's status.
    top_ids = [s["id"] for s in doc.get("steps", []) if isinstance(s, dict) and "id" in s]
    return RunResult(
        run_id=run_id,
        workflow_name=name,
        status=run_status,
        outputs={sid: rc.outputs[sid] for sid in top_ids if sid in rc.outputs},
        terminal_step=terminal,
        terminal_output=terminal_output,
        error=run_error,
        steps=dict(rc.statuses),
    )


def _dispatch(
    ctx: StepContext,
    registry: Mapping[str, ScriptedStep],
    runner: AgentStepRunner,
) -> StepResult:
    if ctx.kind == "agent":
        result = runner.run(ctx)
        sr = result if isinstance(result, StepResult) else StepResult(outputs=dict(result))
        # Add schema defaults beneath sparse runner output so downstream references remain
        # resolvable; explicit runner values always win.
        schema = ctx.step.get("output_schema") if isinstance(ctx.step, dict) else None
        if schema:
            from rebar.llm import contracts

            defaults = {
                k: v for k, v in contracts.default_outputs(schema).items() if k not in sr.outputs
            }
            if defaults:
                sr = StepResult(
                    outputs={**defaults, **sr.outputs}, status=sr.status, error=sr.error
                )
        # Offline/canned runners omit conditional provider_provenance. Supply None so gate
        # wiring resolves it and downstream verdicts still represent the record as absent.
        if "provider_provenance" not in sr.outputs:
            sr = StepResult(
                outputs={**sr.outputs, "provider_provenance": None},
                status=sr.status,
                error=sr.error,
            )
        return sr
    name = ctx.step.get("uses")
    handler = registry.get(name) if name is not None else None
    if handler is None:
        raise WorkflowUnknownStepError(f"unknown scripted step {name!r} (not in the step registry)")
    out = handler(ctx)
    if isinstance(out, StepResult):
        return out
    return StepResult(outputs=dict(out) if isinstance(out, dict) else {})


def _step_record(
    run_id: str,
    step_id: str,
    kind: str,
    result: StepResult,
    captured: Mapping[str, Any] | None = None,
    *,
    frame_key: str | None = None,
    iteration: int | None = None,
    duration_ms: float | None = None,
) -> dict[str, Any]:
    # ``frame_key`` defaults to ``step_id`` (the top frame), so the reducer keys a
    # leaf-only run exactly as v1 did; nested executions pass the full path.
    record: dict[str, Any] = {
        "run_id": run_id,
        "step_id": step_id,
        "frame_key": frame_key or step_id,
        "iteration": iteration,
        "kind": kind,
        "status": result.status,
        "outputs": dict(result.outputs),
        "error": result.error,
        "captured": dict(captured or {}),
    }
    # Per-step wall-clock (toy-kink-ire): present only for leaf steps the interpreter
    # timed; control-frame markers omit it. Additive — absent on a record means "untimed".
    if duration_ms is not None:
        record["duration_ms"] = duration_ms
    return record


# ── Snapshot TTL sweep ────────────────────────────────────────────────────────

# Runs normally tear down snapshots; this lifecycle backstop removes crash orphans.
SNAPSHOT_DIR_NAME = ".rebar/run_snapshots"
SNAPSHOT_TTL_SECONDS = 24 * 3600  # a day; far longer than any run, short enough to GC


def snapshot_root(repo_root: str | None = None) -> Path:
    """The directory under which per-run filesystem snapshots live."""
    base = Path(repo_root) if repo_root else Path.cwd()
    return base / SNAPSHOT_DIR_NAME


def sweep_orphan_snapshots(
    repo_root: str | None = None, *, ttl_seconds: int = SNAPSHOT_TTL_SECONDS
) -> list[str]:
    """Best-effort removal of snapshot directories older than ``ttl_seconds``.

    Returns removed paths, skips failures, and is safe to repeat before each run.
    """
    # Published snapshot trees are chmod'd read-only, so a plain rmtree can fail to
    # remove them (and ignore_errors would silently leak the cache). Restore write
    # bits first, via the same helper snapshot extraction uses to tear down temps.
    from .snapshot import _rmtree_writable

    root = snapshot_root(repo_root)
    if not root.is_dir():
        return []
    cutoff = time.time() - ttl_seconds
    removed: list[str] = []
    for entry in root.iterdir():
        try:
            if entry.stat().st_mtime >= cutoff:
                continue
            if entry.is_dir():
                _rmtree_writable(entry)
            else:
                entry.unlink()
            removed.append(str(entry))
        except OSError:
            continue
    return removed


__all__ = [
    "SNAPSHOT_DIR_NAME",
    "SNAPSHOT_TTL_SECONDS",
    "STEP_REGISTRY",
    "AgentStepRunner",
    "BatchRunRequest",
    "BatchRunResult",
    "BatchRunner",
    "DefaultBatchRunner",
    "ExpressionError",
    "FakeAgentRunner",
    "MemoryRecorder",
    "RunRecorder",
    "RunResult",
    "RunState",
    "ScriptedStep",
    "StepContext",
    "StepContract",
    "StepResult",
    "TicketEventRecorder",
    "contract_for",
    "new_run_id",
    "register_step",
    "resolve_value",
    "run_workflow",
    "shallow_contract_check",
    "snapshot_root",
    "static_order",
    "sweep_orphan_snapshots",
]
