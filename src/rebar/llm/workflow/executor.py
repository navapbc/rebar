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
import shutil
import time
import uuid as _uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
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


# ── Run identity + non-determinism capture (WS-C3) ───────────────────────────


def new_run_id() -> str:
    """A globally-unique, sortable run id: ``{ns-timestamp}-{uuid4hex}``.

    The time prefix makes runs sort newest-last (handy for listing/sweeps); the
    uuid suffix guarantees global uniqueness even across clones writing
    concurrently. Generated ONCE per run and persisted on every WORKFLOW_RUN/STEP
    event so the whole run is keyed identically on every clone.
    """
    return f"{time.time_ns()}-{_uuid.uuid4().hex}"


def _capture_nondeterminism() -> dict[str, Any]:
    """Snapshot the wall clock + a fresh uuid ONCE for a step (WS-C3).

    Persisted in the WORKFLOW_STEP record so a later status/result read sees the
    same values the step ran with, and so a side-effecting step has a stable
    clock/uuid to pair with its (run_id, step_id) idempotency token rather than
    reading the live clock on each retry.
    """
    return {"now_ns": time.time_ns(), "uuid": _uuid.uuid4().hex}


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
    # Non-determinism captured ONCE for this step (now_ns + a fresh step uuid),
    # persisted in the WORKFLOW_STEP record so a status/result read reflects what
    # actually happened (WS-C3). A side-effecting step uses (run_id, step_id) as its
    # downstream idempotency token; ``captured`` gives it a stable clock/uuid.
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


class TicketEventRecorder(RunRecorder):
    """Durable run-state on the target ticket's event log (WS-C3).

    Each call appends a WORKFLOW_RUN/WORKFLOW_STEP event (per-key LWW, WS-C1) to the
    target ticket. The executor calls ``step_recorded`` AFTER a step's effect
    commits — the marker-after-effect rule: a crash between effect and marker leaves
    the effect *applied but unmarked*, so forward-only recovery re-runs the step,
    which is safe because side-effecting steps are idempotent on (run_id, step_id).
    ``completed_step`` reads the persisted marker so a resumed run skips steps that
    DID get marked. All store imports are lazy so the module stays import-light.
    """

    def __init__(self, target_ticket: str, repo_root: str | None = None) -> None:
        self.ticket = target_ticket
        self.repo_root = repo_root

    def _append(self, event_type: str, data: dict[str, Any]) -> None:
        from rebar._commands import _seam

        tracker = _seam.tracker_dir(self.repo_root)
        _seam.append_event(self.ticket, event_type, data, tracker, repo_root=self.repo_root)

    def run_started(self, record: dict[str, Any]) -> None:
        self._append("WORKFLOW_RUN", record)

    def run_finished(self, record: dict[str, Any]) -> None:
        self._append("WORKFLOW_RUN", record)

    def step_recorded(self, record: dict[str, Any]) -> None:
        self._append("WORKFLOW_STEP", record)

    def completed_step(self, run_id: str, step_id: str) -> dict[str, Any] | None:
        from rebar._commands import _seam
        from rebar.reducer import reduce_ticket

        tracker = _seam.tracker_dir(self.repo_root)
        try:
            state = reduce_ticket(str(Path(tracker) / self.ticket))
        except Exception:
            return None
        if not state:
            return None
        return state.get("workflow_steps", {}).get(run_id, {}).get(step_id)


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
    run_id: str | None = None,
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
    the run. Returns a :class:`RunResult` whose terminal-step output is the run's
    result.

    ``run_id`` defaults to a fresh globally-unique id. When ``target_ticket`` is set
    and no ``recorder`` is given, run-state persists durably via a
    :class:`TicketEventRecorder` (so ``get_workflow_status/result`` can read it back
    and a crashed run can resume idempotently); otherwise it stays in memory.
    """
    run_id = run_id or new_run_id()
    registry = STEP_REGISTRY if scripted_registry is None else scripted_registry
    runner = FakeAgentRunner() if agent_runner is None else agent_runner
    if recorder is None:
        recorder = (
            TicketEventRecorder(target_ticket, repo_root) if target_ticket else MemoryRecorder()
        )
    rec = recorder
    secrets = secrets or {}
    inputs = dict(inputs or {})

    # Block only on real errors: the informational "note:" line (degraded
    # jsonschema-absent path) and lint warnings never stop a run.
    schema_errors = [e for e in validate_document(doc) if not e.startswith("note:")]
    lint_errors = [str(f) for f in lint_document(doc) if f.severity != "warning"]
    errors = schema_errors + lint_errors
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

        # Capture non-determinism ONCE, before the effect, so the same clock/uuid
        # is handed to the step and persisted in its marker (WS-C3).
        captured = _capture_nondeterminism()
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
                captured=captured,
            )
            result = _dispatch(ctx, registry, runner)
        except Exception as exc:  # a step failure is data, not a crash
            result = StepResult(outputs={}, status="failed", error=str(exc))

        state = state.with_step(step_id, result)
        # Marker AFTER the effect: a crash between the effect and this line leaves
        # the step applied-but-unmarked, so recovery re-runs it (idempotent on
        # (run_id, step_id)); a present marker means definitely-done.
        rec.step_recorded(_step_record(run_id, step_id, kind, result, captured))

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


def _step_record(
    run_id: str,
    step_id: str,
    kind: str,
    result: StepResult,
    captured: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "run_id": run_id,
        "step_id": step_id,
        "kind": kind,
        "status": result.status,
        "outputs": dict(result.outputs),
        "error": result.error,
        "captured": dict(captured or {}),
    }


# ── Snapshot TTL sweep (WS-C3 owns the sweep; WS-D owns create + teardown) ─────

# Conventional location for git-ref filesystem snapshots a run creates (WS-D). The
# sweep is here (run-lifecycle concern); WS-D writes into this directory and
# normally tears its own snapshot down in a finally — the sweep is the backstop for
# the crash case (a run that died before teardown leaves an orphan).
SNAPSHOT_DIR_NAME = ".rebar/run_snapshots"
SNAPSHOT_TTL_SECONDS = 24 * 3600  # a day; far longer than any run, short enough to GC


def snapshot_root(repo_root: str | None = None) -> Path:
    """The directory under which per-run filesystem snapshots live."""
    base = Path(repo_root) if repo_root else Path.cwd()
    return base / SNAPSHOT_DIR_NAME


def sweep_orphan_snapshots(
    repo_root: str | None = None, *, ttl_seconds: int = SNAPSHOT_TTL_SECONDS
) -> list[str]:
    """Remove snapshot tmpdirs older than ``ttl_seconds`` (orphans from crashed
    runs). Returns the list of removed paths. Best-effort: an unremovable entry is
    skipped, not raised — the sweep must never break a run. Idempotent and safe to
    call at the start of every run.
    """
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
                shutil.rmtree(entry, ignore_errors=True)
            else:
                entry.unlink()
            removed.append(str(entry))
        except OSError:
            continue
    return removed


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
    "TicketEventRecorder",
    "RunState",
    "RunResult",
    "ExpressionError",
    "resolve_value",
    "static_order",
    "run_workflow",
    "new_run_id",
    "snapshot_root",
    "sweep_orphan_snapshots",
    "SNAPSHOT_DIR_NAME",
    "SNAPSHOT_TTL_SECONDS",
]
