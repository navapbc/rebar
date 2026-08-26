"""Completion-verifier consumption and workflow phase metrics.

The workflow interpreter already records ``duration_ms`` for every dispatched leaf
step.  Completion telemetry should reuse those observations instead of adding timing
hooks to the shared interpreter (which also runs plan review and code review).  This
module owns the bounded, in-memory reduction from those recorder rows to sidecar
fields, alongside the older request/tool/LLM consumption reduction.

``verifier_workflow_ms`` remains the backward-compatible aggregate around
``run_workflow``.  Its non-overlapping partition is:

* ``verifier_precheck_context_ms`` — the ``precheck`` operation: child/bug closure
  checks plus ticket context and declared-file prefetch assembly;
* ``verifier_completion_agent_ms`` — the ``verify`` agent step, including its model
  calls, repository tools, bounded recovery, and finalizer;
* ``verifier_verdict_reconcile_ms`` — deterministic normalization, citation
  resolution, and verdict invariant enforcement;
* ``verifier_no_llm_passthrough_ms`` — the deterministic short-circuit verdict path;
* ``verifier_unclassified_workflow_steps_ms`` — timed leaf ids introduced without a
  telemetry mapping, kept visible rather than silently folded into overhead; and
* ``verifier_workflow_residual_ms`` — aggregate time outside timed leaf dispatches:
  workflow validation, expression/contract resolution, branch routing, recorder
  bookkeeping, terminal-result assembly, and integer-ms rounding.

The reduction performs no I/O and makes no clock reads.  The dispatcher supplies the
one elapsed interval it already measured, so the added runtime work is one pass over
the handful of in-memory recorder rows plus dictionary construction.
"""

from __future__ import annotations

from typing import Any


_WORKFLOW_STEP_FIELDS = {
    "precheck": "verifier_precheck_context_ms",
    "verify": "verifier_completion_agent_ms",
    "reconcile": "verifier_verdict_reconcile_ms",
    "passthrough": "verifier_no_llm_passthrough_ms",
}
_UNCLASSIFIED_FIELD = "verifier_unclassified_workflow_steps_ms"
_RESIDUAL_FIELD = "verifier_workflow_residual_ms"
_WORKFLOW_TOTAL_FIELD = "verifier_workflow_ms"
_MILLIS_IN_NANO = 1_000_000


def _step_duration_ns(step: Any) -> int | None:
    """Return one recorder row's rounded non-negative duration in nanoseconds."""
    if not isinstance(step, dict):
        return None
    duration_ms = step.get("duration_ms")
    if not isinstance(duration_ms, (int, float)) or duration_ms < 0:
        return None
    return round(duration_ms * _MILLIS_IN_NANO)


def _workflow_step_partition(rec_steps: list[Any]) -> dict[str, int]:
    """Classify timed leaf records by completion operation, in integer milliseconds.

    Flooring happens per published partition.  The residual is computed from those
    same integer values, so every normal record reconciles exactly at the serialized
    precision rather than making the probe infer a cause from floating-point drift.
    """
    duration_ns = {field: 0 for field in _WORKFLOW_STEP_FIELDS.values()}
    unclassified_ns = 0
    for step in rec_steps:
        elapsed_ns = _step_duration_ns(step)
        if elapsed_ns is None:
            continue
        field = _WORKFLOW_STEP_FIELDS.get(step.get("step_id"))
        if field is None:
            unclassified_ns += elapsed_ns
        else:
            duration_ns[field] += elapsed_ns
    return {
        **{field: elapsed_ns // _MILLIS_IN_NANO for field, elapsed_ns in duration_ns.items()},
        _UNCLASSIFIED_FIELD: unclassified_ns // _MILLIS_IN_NANO,
    }


def attach_completion_workflow_phases(
    metrics: dict[str, int], rec_steps: list[Any], workflow_elapsed_ns: int
) -> None:
    """Accumulate the concrete, non-overlapping completion workflow partition."""
    workflow_ms = max(0, workflow_elapsed_ns // _MILLIS_IN_NANO)
    partition = _workflow_step_partition(rec_steps)
    residual_ms = max(0, workflow_ms - sum(partition.values()))
    partition[_RESIDUAL_FIELD] = residual_ms
    for field, value in partition.items():
        metrics[field] = metrics.get(field, 0) + value
    metrics[_WORKFLOW_TOTAL_FIELD] = metrics.get(_WORKFLOW_TOTAL_FIELD, 0) + workflow_ms


def _sum_run_consumption(rec_steps: list[Any]) -> dict[str, Any] | None:
    """Aggregate requests, tools, and timed agent/deterministic work from a run."""
    requests = 0
    tool_calls = 0
    llm_ms = 0.0
    det_ms = 0.0
    llm_calls = 0
    saw_usage = False
    for step in rec_steps:
        if not isinstance(step, dict) or step.get("status") != "succeeded":
            continue
        duration_ms = step.get("duration_ms")
        outputs = step.get("outputs")
        outputs = outputs if isinstance(outputs, dict) else {}
        if step.get("kind") == "agent":
            llm_calls += 1
            if isinstance(duration_ms, (int, float)):
                llm_ms += duration_ms
            usage = outputs.get("_usage")
            if isinstance(usage, dict) and usage:
                saw_usage = True
                requests += int(usage.get("requests") or 0)
                tool_calls += int(usage.get("tool_calls") or 0)
        elif isinstance(duration_ms, (int, float)):
            det_ms += duration_ms
    if not saw_usage:
        return None
    return {
        "requests": requests,
        "tool_calls": tool_calls,
        "llm_calls": llm_calls,
        "llm_ms": round(llm_ms, 1),
        "det_ms": round(det_ms, 1),
    }


def _attach_completion_metrics(verdict: dict[str, Any], rec: Any, total_ms: float) -> None:
    """Attach consumed request/tool/time totals when an agent step actually ran."""
    consumed = _sum_run_consumption(getattr(rec, "steps", []) or [])
    if consumed is None:
        return
    verdict["metrics"] = {**consumed, "total_ms": round(total_ms, 1)}


def _add_phase(metrics: dict[str, int], key: str, elapsed_ns: int) -> None:
    """Accumulate an existing monotonic interval at sidecar integer-ms precision."""
    metrics[key] = metrics.get(key, 0) + elapsed_ns // _MILLIS_IN_NANO


__all__ = [
    "_add_phase",
    "_attach_completion_metrics",
    "_sum_run_consumption",
    "attach_completion_workflow_phases",
]
