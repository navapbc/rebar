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
import time
import uuid as _uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any

from rebar.llm.errors import WorkflowError, WorkflowValidationError

from .lint import lint_document
from .schema import validate_document

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
    """Snapshot the non-deterministic inputs a step may use — the wall clock, a
    fresh uuid, and a random seed — ONCE per step EXECUTION (WS-C3).

    Persisted in that execution's WORKFLOW_STEP record, so a later status/result
    read replays the exact values the step ran with, and a step that needs a
    clock/uuid/seed reads them from ``ctx.captured`` instead of a live source.
    Note the scope: the capture is per execution, not immortal — a step that
    crashed BEFORE its marker committed legitimately re-executes (forward-only
    recovery) and captures afresh; effectively-once then rests on the effect being
    idempotent over (run_id, step_id), which is the step's contract, not the
    clock's. (Live external reads a step performs are the step's own concern; the
    seed/clock/uuid are the engine-provided non-determinism.)
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
    # The full FRAME KEY of this execution (``step_id`` at the top frame; a path like
    # ``L#2/attempt`` inside a loop/map iteration). It is the durable, iteration-aware
    # idempotency token — a side-effecting step inside a loop/map uses
    # ``(run_id, frame_key)`` (not ``(run_id, step_id)``, which repeats every
    # iteration) so each iteration's effect is distinct yet replay-stable (WS-C3 / v2).
    frame_key: str = ""
    # The immediate enclosing loop/map iteration index (None at the top frame). Carried
    # so a step can read its own iteration; the full path is in ``frame_key``.
    iteration: int | None = None
    # Non-determinism captured ONCE for this step execution (now_ns + a fresh uuid
    # + a random seed), persisted in the WORKFLOW_STEP record so a status/result
    # read replays what actually happened (WS-C3). A side-effecting step uses
    # (run_id, step_id) as its downstream idempotency token; ``captured`` gives it a
    # clock/uuid/seed to read instead of a live source.
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


@dataclass(frozen=True)
class StepContract:
    """A step kind's authored I/O CONTRACT (workflow authoring v2, 5e78).

    Every step kind should advertise an INPUT contract, an OUTPUT contract, and a
    description so the editor can present a typed palette + inspector and the linter
    can check a `${{ steps.<id>.outputs.<name> }}` reference against the producing
    step's declared outputs. ``input_schema`` / ``output_schema`` are SCHEMA NAMES
    (resolvable via :mod:`rebar.schemas`), not inline schemas; either may be ``None``
    for a step whose I/O is not yet annotated (the linter then treats that step's
    outputs as UNKNOWN and never flags a reference to them)."""

    input_schema: str | None = None
    output_schema: str | None = None
    description: str = ""


# The contracts registered alongside scripted steps. Keyed by the same `uses` name as
# STEP_REGISTRY; a name absent here is a step with no declared contract (UNKNOWN).
STEP_CONTRACTS: dict[str, StepContract] = {}


def register_step(
    name: str,
    *,
    input_schema: str | None = None,
    output_schema: str | None = None,
    description: str | None = None,
) -> Callable[[ScriptedStep], ScriptedStep]:
    """Decorator: register a scripted step under ``name`` (used by WS-E).

    The optional ``input_schema`` / ``output_schema`` (schema NAMES) and
    ``description`` declare the step's contract (workflow authoring v2). When any is
    given a :class:`StepContract` is recorded in ``STEP_CONTRACTS`` and exposed via
    :func:`contract_for` — consumed by the editor inspector and the reference linter.
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


def contract_for(step_name: str) -> StepContract | None:
    """The declared :class:`StepContract` for a scripted step ``name``, or ``None``
    when the step is unregistered or declares no contract (UNKNOWN to the linter)."""
    return STEP_CONTRACTS.get(step_name)


class AgentStepRunner:
    """The agentic-step seam (the real pydantic_ai-backed runner plugs in here)."""

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

    def completed_step(self, run_id: str, frame_key: str) -> dict[str, Any] | None:
        """Return a prior SUCCEEDED record for this FRAME KEY (idempotent skip), or
        None. ``frame_key`` is the bare ``step_id`` at the top frame or an
        iteration-embedding path inside a loop/map body.

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

    def completed_step(self, run_id: str, frame_key: str) -> dict[str, Any] | None:
        from rebar._commands import _seam
        from rebar.reducer import reduce_ticket

        tracker = _seam.tracker_dir(self.repo_root)
        try:
            state = reduce_ticket(str(Path(tracker) / self.ticket))
        except Exception:
            return None
        if not state:
            return None
        return state.get("workflow_steps", {}).get(run_id, {}).get(frame_key)


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
    # A bare `if:` value with no `${{ … }}` is NOT an expression — under naive
    # substitution it resolves to the literal string and is silently truthy
    # (the GHA `if: steps.a.outputs.ok` footgun). The linter rejects this so a
    # well-formed workflow never reaches here with one; if internals are driven
    # directly past the lint, fail closed (skip) rather than always-run.
    if isinstance(guard, str) and "${{" not in guard:
        return False
    try:
        val = resolve_value(guard, state, secrets)
    except ExpressionError:
        return False
    if isinstance(val, str):
        return val.strip().lower() not in ("", "false", "0", "no")
    return bool(val)


# ── The v2 worklist interpreter ───────────────────────────────────────────────
# The recursive frame walk (branch/loop/map + the frame-scoped resolver + _RunCtx)
# lives in :mod:`rebar.llm.workflow.interpreter` (kept under the module-size cap and
# scanned by the same Burr tripwire). ``run_workflow`` imports it lazily below to
# avoid an import cycle (interpreter imports the step interfaces from here).


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
    terminal = _terminal_step(doc)

    rec.run_started(
        {"run_id": run_id, "workflow_name": name, "status": "running", "inputs": inputs}
    )

    # Walk the IR frame by frame (the v2 worklist interpreter). For a leaf-only
    # (migrated-v1) workflow this degenerates to the old linear pass: the top frame's
    # keys ARE the bare step ids (frame_key == step_id), so the recorded markers and
    # the RunResult below are byte-compatible with the v1 path. Imported lazily — the
    # interpreter imports the step interfaces from here, so a module-level import
    # would cycle.
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
    )
    _execute_frame(rc, list(doc.get("steps", [])), ("",), {}, None)

    run_status = "failed" if rc.failed else "succeeded"
    run_error = rc.error
    terminal_output = rc.outputs.get(terminal) if terminal else None
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
    *,
    frame_key: str | None = None,
    iteration: int | None = None,
) -> dict[str, Any]:
    # ``frame_key`` defaults to ``step_id`` (the top frame), so the reducer keys a
    # leaf-only run exactly as v1 did; nested executions pass the full path.
    return {
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
