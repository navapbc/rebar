"""The v2 worklist interpreter — conditionals (branch), bounded loops, and map.

The executor (:mod:`rebar.llm.workflow.executor`) owns run identity, the step
interfaces, the persistence recorders, and the ``run_workflow`` entry point; THIS
module owns the recursive frame walk that replaced v1's single linear
``static_order`` pass. It recurses into a ``branch``'s then/else, a ``loop``'s body,
and a ``map``'s body, staying SYNCHRONOUS and single-threaded so the Burr tripwire
(``tests/unit/workflow/test_executor_tripwire.py``, which scans THIS file too) holds
— bounded-concurrent fan-out is a separate, narrowly-scoped story.

The POC discipline is load-bearing: ALL control-flow state (loop position, which
branch, map index) is DERIVED from RECORDED OUTPUTS, never mutated out of band — so
replay re-derives identical decisions and side effects are exactly-once. Each leaf
execution is keyed by its full FRAME KEY (a path like ``L#2/attempt`` embedding the
loop/map iteration), so a step that runs once per iteration gets a distinct,
replay-stable idempotency marker (the (run_id, step_id, iteration) keying).

Burr-adoption trigger list / tripwire: see :mod:`rebar.llm.workflow.executor` — the
same armed constraints apply here (no asyncio / threading / multiprocessing / retry
library). Adopt Burr per that list before adding concurrency; do not grow a
scheduler in this synchronous walk.
"""

from __future__ import annotations

import graphlib
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from .executor import (
    _ENV_RE,
    _EXPR_RE,
    _INPUT_RE,
    _SECRET_RE,
    _STEP_OUT_RE,
    AgentStepRunner,
    ExpressionError,
    RunRecorder,
    ScriptedStep,
    StepContext,
    StepResult,
    _capture_nondeterminism,
    _dispatch,
    _step_record,
)
from .schema import step_kind

# ── The v2 worklist interpreter (conditionals / loops / map) ──────────────────
#
# Replaces the v1 single linear ``static_order`` pass: the engine now walks the IR
# FRAME BY FRAME, recursing into a ``branch``'s then/else, a ``loop``'s body, and a
# ``map``'s body. It stays SYNCHRONOUS and single-threaded — the Burr tripwire holds
# (bounded-concurrent fan-out is a separate, narrowly-scoped story). The POC
# discipline is load-bearing: ALL control-flow state (loop position, which branch,
# map index) is DERIVED from RECORDED OUTPUTS, never mutated out of band — so replay
# re-derives identical decisions and side effects are exactly-once. Each leaf
# execution is keyed by its full FRAME KEY (a path like ``L#2/attempt`` embedding the
# loop/map iteration), so a step that runs once per iteration gets a distinct,
# replay-stable idempotency marker.

_LOOP_VAR_RE = re.compile(r"^loop\.([A-Za-z_][A-Za-z0-9_-]*)$")
_MAP_VAR_RE = re.compile(r"^map\.([A-Za-z_][A-Za-z0-9_-]*)$")


def _truthy(val: Any) -> bool:
    """The engine's truthiness rule (shared by guards + control conditions): a string
    is falsy only when empty/``false``/``0``/``no`` (case-insensitive)."""
    if isinstance(val, str):
        return val.strip().lower() not in ("", "false", "0", "no")
    return bool(val)


def _resolve_one_scoped(
    expr: str,
    *,
    inputs: Mapping[str, Any],
    outputs: Mapping[str, Mapping[str, Any]],
    prefixes: tuple[str, ...],
    bindings: Mapping[str, Any],
    secrets: Mapping[str, str],
) -> Any:
    """Resolve one ``${{ … }}`` body against the FRAME SCOPE.

    A ``steps.<id>.outputs.<name>`` reference is matched against ``prefixes``
    innermost-first — so a same-frame output shadows an enclosing one, exactly as the
    linter scopes it. ``loop.<var>`` / ``map.<as>`` read the frame ``bindings``.
    """
    expr = expr.strip()
    m = _INPUT_RE.match(expr)
    if m:
        name = m.group(1)
        if name not in inputs:
            raise ExpressionError(f"input {name!r} is not set for this run")
        return inputs[name]
    m = _STEP_OUT_RE.match(expr)
    if m:
        step, out = m.group(1), m.group(2)
        for p in prefixes:
            fk = f"{p}{step}"
            if fk in outputs:
                if out not in outputs[fk]:
                    raise ExpressionError(f"step {step!r} did not produce output {out!r}")
                return outputs[fk][out]
        raise ExpressionError(f"step {step!r} has not produced outputs yet")
    m = _LOOP_VAR_RE.match(expr)
    if m:
        key = f"loop.{m.group(1)}"
        if key not in bindings:
            raise ExpressionError(f"loop variable {expr!r} is not in scope")
        return bindings[key]
    m = _MAP_VAR_RE.match(expr)
    if m:
        key = f"map.{m.group(1)}"
        if key not in bindings:
            raise ExpressionError(f"map binding {expr!r} is not in scope")
        return bindings[key]
    m = _SECRET_RE.match(expr)
    if m:
        name = m.group(1)
        if name not in secrets:
            raise ExpressionError(f"secret {name!r} is not available")
        return secrets[name]
    raise ExpressionError(f"unresolvable expression {expr!r}")


def _resolve_scoped(
    value: Any,
    *,
    inputs: Mapping[str, Any],
    outputs: Mapping[str, Mapping[str, Any]],
    prefixes: tuple[str, ...],
    bindings: Mapping[str, Any],
    secrets: Mapping[str, str],
) -> Any:
    """Frame-scoped sibling of :func:`resolve_value` (recursive over dict/list); a
    string that is EXACTLY one expression resolves to the raw referenced value."""
    kw = dict(inputs=inputs, outputs=outputs, prefixes=prefixes, bindings=bindings, secrets=secrets)
    if isinstance(value, dict):
        return {k: _resolve_scoped(v, **kw) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve_scoped(v, **kw) for v in value]
    if not isinstance(value, str):
        return value
    whole = _EXPR_RE.fullmatch(value)
    if whole:
        return _resolve_one_scoped(whole.group(1), **kw)
    out = _EXPR_RE.sub(lambda m: str(_resolve_one_scoped(m.group(1), **kw)), value)

    def sub_env(m: re.Match[str]) -> str:
        var = m.group(1)
        if var not in os.environ:
            raise ExpressionError(f"environment variable {var!r} is not set")
        return os.environ[var]

    return _ENV_RE.sub(sub_env, out)


@dataclass
class _RunCtx:
    """Mutable run state threaded through the recursive interpreter. ``outputs`` /
    ``statuses`` are keyed by FRAME KEY (``step_id`` at the top frame); ``failed`` /
    ``error`` short-circuit the walk on the first failed step."""

    run_id: str
    doc: Mapping[str, Any]
    registry: Mapping[str, ScriptedStep]
    runner: AgentStepRunner
    rec: RunRecorder
    secrets: Mapping[str, str]
    inputs: Mapping[str, Any]
    target_ticket: str | None
    repo_root: str | None
    outputs: dict[str, dict[str, Any]] = field(default_factory=dict)
    statuses: dict[str, str] = field(default_factory=dict)
    failed: bool = False
    error: str | None = None


def _frame_order(steps: list[Mapping[str, Any]]) -> list[str]:
    """Topological execution order WITHIN one frame (``needs`` edges that point
    outside the frame are ignored — the linter rejects them)."""
    ids = {s["id"] for s in steps if isinstance(s, dict) and "id" in s}
    graph: dict[str, set[str]] = {}
    for s in steps:
        if not isinstance(s, dict) or "id" not in s:
            continue
        sid = s["id"]
        graph[sid] = {
            n for n in (s.get("needs") or []) if isinstance(n, str) and n in ids and n != sid
        }
    return list(graphlib.TopologicalSorter(graph).static_order())


def _guard_scoped(
    step: Mapping[str, Any], rc: _RunCtx, prefixes: tuple[str, ...], bindings: Mapping[str, Any]
) -> bool:
    """The ``if:`` skip-guard, evaluated in the frame scope. A bare (non-expression)
    guard fails closed (skip), mirroring the v1 rule."""
    guard = step.get("if")
    if not guard:
        return True
    if isinstance(guard, str) and "${{" not in guard:
        return False
    try:
        val = _resolve_scoped(
            guard,
            inputs=rc.inputs,
            outputs=rc.outputs,
            prefixes=prefixes,
            bindings=bindings,
            secrets=rc.secrets,
        )
    except ExpressionError:
        return False
    return _truthy(val)


def _execute_frame(
    rc: _RunCtx,
    steps: list[Mapping[str, Any]],
    prefixes: tuple[str, ...],
    bindings: Mapping[str, Any],
    iteration: int | None,
) -> None:
    """Execute one frame's steps in ``needs`` order, recursing into control
    constructs. ``prefixes[0]`` is the current frame's full key prefix; ``iteration``
    is the enclosing loop/map index (None at the top)."""
    by_id = {s["id"]: s for s in steps if isinstance(s, dict) and "id" in s}
    cur = prefixes[0]
    for sid in _frame_order(steps):
        if rc.failed:
            return
        step = by_id[sid]
        kind = step_kind(step)
        frame_key = f"{cur}{sid}"
        if not _guard_scoped(step, rc, prefixes, bindings):
            rc.outputs[frame_key] = {}
            rc.statuses[frame_key] = "skipped"
            rc.rec.step_recorded(
                _step_record(
                    rc.run_id,
                    sid,
                    kind,
                    StepResult(status="skipped"),
                    frame_key=frame_key,
                    iteration=iteration,
                )
            )
            continue
        if kind in ("scripted", "agent"):
            _run_leaf(rc, step, sid, frame_key, kind, prefixes, bindings, iteration)
        elif kind == "branch":
            _run_branch(rc, step, sid, frame_key, prefixes, bindings, iteration)
        elif kind == "loop":
            _run_loop(rc, step, sid, frame_key, cur, prefixes, bindings, iteration)
        elif kind == "map":
            _run_map(rc, step, sid, frame_key, cur, prefixes, bindings, iteration)


def _run_leaf(rc, step, sid, frame_key, kind, prefixes, bindings, iteration) -> None:
    """Execute a scripted/agent leaf with iteration-keyed idempotency (WS-C3 / v2):
    skip if a committed marker exists for this FRAME KEY (replaying its output), else
    run, record a 'running' marker before and the durable marker after the effect."""
    prior = rc.rec.completed_step(rc.run_id, frame_key)
    if prior is not None and prior.get("status") == "succeeded":
        rc.outputs[frame_key] = dict(prior.get("outputs", {}))
        rc.statuses[frame_key] = "succeeded"
        return
    rc.rec.step_recorded(
        {
            "run_id": rc.run_id,
            "step_id": sid,
            "frame_key": frame_key,
            "iteration": iteration,
            "kind": kind,
            "status": "running",
            "outputs": {},
            "error": None,
        }
    )
    captured = _capture_nondeterminism()
    try:
        resolved = _resolve_scoped(
            dict(step.get("with") or {}),
            inputs=rc.inputs,
            outputs=rc.outputs,
            prefixes=prefixes,
            bindings=bindings,
            secrets=rc.secrets,
        )
        ctx = StepContext(
            run_id=rc.run_id,
            step_id=sid,
            kind=kind,
            step=step,
            inputs=resolved,
            workflow=rc.doc,
            target_ticket=rc.target_ticket,
            repo_root=rc.repo_root,
            captured=captured,
            frame_key=frame_key,
            iteration=iteration,
        )
        result = _dispatch(ctx, rc.registry, rc.runner)
    except Exception as exc:  # a step failure is data, not a crash
        result = StepResult(outputs={}, status="failed", error=str(exc))
    rc.outputs[frame_key] = dict(result.outputs)
    rc.statuses[frame_key] = result.status
    rc.rec.step_recorded(
        _step_record(
            rc.run_id, sid, kind, result, captured, frame_key=frame_key, iteration=iteration
        )
    )
    if result.status == "failed":
        rc.failed = True
        rc.error = f"step {frame_key!r} failed: {result.error}"


def _maybe_record_control(rc, sid, frame_key, iteration, kind, outputs) -> None:
    """Record a control step's completion marker AFTER its body ran, unless already
    committed (so replay doesn't re-append). A control step has no side effect; the
    marker exists for status visibility + so replay knows the frame fully completed."""
    prior = rc.rec.completed_step(rc.run_id, frame_key)
    if prior is not None and prior.get("status") == "succeeded":
        return
    rc.rec.step_recorded(
        _step_record(
            rc.run_id,
            sid,
            kind,
            StepResult(outputs=outputs),
            frame_key=frame_key,
            iteration=iteration,
        )
    )


def _run_branch(rc, step, sid, frame_key, prefixes, bindings, iteration) -> None:
    """Evaluate ``when`` (in this frame's scope) and execute the chosen then/else
    frame. The decision is DERIVED from recorded outputs, so replay re-routes
    identically."""
    branch = step.get("branch") or {}
    try:
        taken = _truthy(
            _resolve_scoped(
                branch.get("when"),
                inputs=rc.inputs,
                outputs=rc.outputs,
                prefixes=prefixes,
                bindings=bindings,
                secrets=rc.secrets,
            )
        )
    except ExpressionError as exc:
        rc.failed = True
        rc.error = f"branch {frame_key!r} condition failed: {exc}"
        return
    arm = "then" if taken else "else"
    rc.outputs[frame_key] = {"taken": arm}
    rc.statuses[frame_key] = "succeeded"
    arm_steps = branch.get(arm)
    if isinstance(arm_steps, list):
        child = (f"{prefixes[0]}{sid}@{arm}/",) + prefixes
        _execute_frame(rc, arm_steps, child, bindings, iteration)
    if not rc.failed:
        _maybe_record_control(rc, sid, frame_key, iteration, "branch", {"taken": arm})


def _loop_should_run(rc, loop, sid, i, cur, prefixes, bindings, var) -> bool:
    """Whether loop iteration ``i`` should run: ``while`` truthy / ``until`` falsy,
    evaluated against the JUST-COMPLETED iteration's recorded body outputs (i-1); with
    neither condition, always True (bounded only by ``max_iterations``)."""
    has_while, has_until = "while" in loop, "until" in loop
    if not has_while and not has_until:
        return True
    cond_prefixes, cond_bindings = prefixes, bindings
    if i > 0:
        cond_prefixes = (f"{cur}{sid}#{i - 1}/",) + prefixes
        cond_bindings = {**bindings, f"loop.{var}": i - 1}
    expr = loop.get("while") if has_while else loop.get("until")
    try:
        val = _truthy(
            _resolve_scoped(
                expr,
                inputs=rc.inputs,
                outputs=rc.outputs,
                prefixes=cond_prefixes,
                bindings=cond_bindings,
                secrets=rc.secrets,
            )
        )
    except ExpressionError:
        # At i==0 there is no prior iteration yet, so a reference to a BODY output
        # legitimately doesn't resolve — treat it as falsy (a `while` doesn't start;
        # an `until` runs its first iteration, the do-while pattern). At i>0 the prior
        # iteration completed (rc.failed is checked after each body), so a reference
        # that fails to resolve is a REAL error — propagate it rather than silently
        # ending the loop.
        if i > 0:
            raise
        val = False
    return val if has_while else (not val)


def _run_loop(rc, step, sid, frame_key, cur, prefixes, bindings, iteration) -> None:
    """Bounded iteration. ``max_iterations`` is the hard cap (the runaway guard): a
    conditioned loop still wanting to continue at the cap is a hard error, never a
    silent stop. The continuation is derived from recorded body outputs, so replay
    reconstructs the exact iteration count."""
    loop = step.get("loop") or {}
    max_iter = loop.get("max_iterations")
    if not isinstance(max_iter, int) or max_iter < 1:
        rc.failed = True
        rc.error = f"loop {frame_key!r} has no valid max_iterations"
        return
    var = loop.get("var") if isinstance(loop.get("var"), str) else "index"
    count, hit_cap = 0, True
    try:
        for i in range(max_iter):
            if not _loop_should_run(rc, loop, sid, i, cur, prefixes, bindings, var):
                hit_cap = False
                break
            child = (f"{cur}{sid}#{i}/",) + prefixes
            _execute_frame(rc, loop.get("body") or [], child, {**bindings, f"loop.{var}": i}, i)
            if rc.failed:
                return
            count = i + 1
        runaway = (
            hit_cap
            and ("while" in loop or "until" in loop)
            and _loop_should_run(rc, loop, sid, max_iter, cur, prefixes, bindings, var)
        )
    except ExpressionError as exc:
        # A genuine mid-loop condition resolution failure (not the i==0 do-while case)
        # — fail the run with the underlying cause, never stop silently.
        rc.failed = True
        rc.error = f"loop {frame_key!r} condition failed: {exc}"
        return
    if runaway:
        rc.failed = True
        rc.error = f"loop {frame_key!r} exceeded max_iterations={max_iter} (runaway guard)"
        return
    rc.outputs[frame_key] = {"iterations": count}
    rc.statuses[frame_key] = "succeeded"
    _maybe_record_control(rc, sid, frame_key, iteration, "loop", {"iterations": count})


def _run_map(rc, step, sid, frame_key, cur, prefixes, bindings, iteration) -> None:
    """Fan-out: run the body once per element of ``over`` (resolved in THIS frame's
    scope, before fan-out), binding each element to ``as``. Serial here — bounded
    concurrency is a separate story — but iteration-keyed so it is order-independent
    on replay."""
    mp = step.get("map") or {}
    try:
        collection = _resolve_scoped(
            mp.get("over"),
            inputs=rc.inputs,
            outputs=rc.outputs,
            prefixes=prefixes,
            bindings=bindings,
            secrets=rc.secrets,
        )
    except ExpressionError as exc:
        rc.failed = True
        rc.error = f"map {frame_key!r} `over` failed: {exc}"
        return
    if not isinstance(collection, (list, tuple)):
        rc.failed = True
        rc.error = (
            f"map {frame_key!r} `over` did not yield a list (got {type(collection).__name__})"
        )
        return
    as_name, index_var = mp.get("as"), mp.get("index_var")
    for j, item in enumerate(collection):
        child = (f"{cur}{sid}#{j}/",) + prefixes
        child_bindings = {**bindings, f"map.{as_name}": item}
        if isinstance(index_var, str):
            child_bindings[f"map.{index_var}"] = j
        _execute_frame(rc, mp.get("body") or [], child, child_bindings, j)
        if rc.failed:
            return
    rc.outputs[frame_key] = {"count": len(collection)}
    rc.statuses[frame_key] = "succeeded"
    _maybe_record_control(rc, sid, frame_key, iteration, "map", {"count": len(collection)})
