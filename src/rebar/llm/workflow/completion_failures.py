"""Failure taxonomy of the completion-recovery pipeline: classify, bound, finalize.

The recovery orchestrator (:mod:`rebar.llm.workflow.completion_recovery`) only ever enters
recovery on a TYPED, non-retryable exhaustion, and only ever reports failure through bounded,
allowlisted metadata. This module owns those decisions:

* :func:`_normalized_finish_reason` / :func:`_is_token_exhaustion` — classify typed output
  exhaustion (budget / runaway-loop / token-cap) without mistaking generic context text.
* :func:`_bounded_diagnostic` — extract ONLY safe, bounded failure metadata from runner
  exceptions (an allowlist of scalar keys plus one sanctioned bounded list; never prompt or
  argument text).
* :func:`raise_completion_workflow_failure` — finalize a failed completion workflow without
  widening dispatch policy: emit the ``gate_error_v1`` sidecar, render the primary run's
  repetition summary, and raise the typed error the close gate expects.
"""

from __future__ import annotations

import re
from typing import Any, NoReturn

from rebar.llm.errors import CompletionRecoveryError, LLMError

from . import executor as _ex


def _normalized_finish_reason(exc: BaseException) -> str:
    """Return allowlisted typed finish metadata, preferring runner diagnostics."""

    outcome = getattr(exc, "outcome", None)
    sources = (
        getattr(exc, "diagnostic", None),
        getattr(outcome, "diagnostic", None),
        outcome,
        getattr(exc, "usage", None),
    )
    for source in sources:
        if isinstance(source, dict):
            value = source.get("finish_reason")
            if isinstance(value, str) and value.strip():
                return re.sub(r"[\s-]+", "_", value.strip().lower())
    value = getattr(exc, "finish_reason", None)
    if isinstance(value, str):
        return re.sub(r"[\s-]+", "_", value.strip().lower())
    return ""


def _is_token_exhaustion(exc: BaseException) -> bool:
    """Classify typed output exhaustion without mistaking generic context text."""

    finish_reason = _normalized_finish_reason(exc)
    if finish_reason in {
        "length",
        "max_tokens",
        "max_output_tokens",
        "context_length_exceeded",
        "context_window_exceeded",
        "context_window_overflow",
        "maximum_context_length_exceeded",
    }:
        return True

    message = str(exc).lower()
    return any(
        marker in message
        for marker in (
            "finish_reason=length",
            "finish_reason: length",
            "max_tokens",
            "max tokens",
            "token cap",
            "context length exceeded",
            "context_length_exceeded",
            "context window exceeded",
            "context-window exceeded",
            "context window overflow",
            "context-window overflow",
            "maximum context length exceeded",
        )
    )


def _bounded_diagnostic(
    exc: BaseException,
    *,
    stage: str,
    total: int,
    completed: int,
) -> dict[str, Any]:
    """Extract only safe, bounded failure metadata from runner exceptions."""

    diagnostic: dict[str, Any] = {
        "stage": stage,
        "exception_type": type(exc).__name__,
        "recovery_attempted": True,
        "criteria_total": total,
        "criteria_completed": completed,
        "trace_id": None,
        "requests": None,
        "tool_calls": None,
        "input_tokens": None,
        "output_tokens": None,
    }
    allowed = {
        "trace_id",
        "finish_reason",
        "requests",
        "request_count",
        "request_limit",
        "tool_calls",
        "tool_calls_limit",
        "tool_calls_distinct",
        "max_consecutive_repeat",
        "top_repeated_tool_calls",
        "distinct_ratio_window",
        "input_tokens",
        "output_tokens",
        "cache_read_tokens",
        "cache_write_tokens",
        "criteria_limit",
        "criterion_index",
        "criterion_chars",
        "criterion_char_limit",
        "criteria_chars",
        "criteria_char_limit",
        "context_chars",
        "context_char_limit",
        "evidence_chars",
        "evidence_char_limit",
        "total_evidence_chars",
        "total_evidence_char_limit",
        "finalizer_input_chars",
        "finalizer_input_char_limit",
        "criteria_unmet",
        "criteria_returned",
        "criteria_exhausted",
        "criteria_completed",
        "coverage_exact",
    }

    def merge(source: object, *, overwrite: bool = False) -> None:
        if not isinstance(source, dict):
            return
        for key, value in source.items():
            if key not in allowed:
                continue
            if key == "top_repeated_tool_calls":
                # The one sanctioned non-scalar: a bounded list of
                # {"signature", "count"} dicts (hashed signatures, no prompt or
                # argument text). Copy it so the diagnostic never aliases the
                # exception's own structure.
                if not isinstance(value, list):
                    continue
                if overwrite or diagnostic.get(key) is None:
                    diagnostic[key] = [
                        dict(item) if isinstance(item, dict) else item for item in value
                    ]
                continue
            if not isinstance(value, (str, int, float, bool)) and value is not None:
                continue
            if overwrite or diagnostic.get(key) is None:
                diagnostic[key] = value

    inherited = getattr(exc, "diagnostic", None)
    merge(inherited, overwrite=True)
    outcome = getattr(exc, "outcome", None)
    outcome_diag = getattr(outcome, "diagnostic", None)
    merge(outcome_diag)
    merge(outcome)
    merge(getattr(exc, "usage", None))
    for key in allowed:
        value = getattr(exc, key, None)
        if isinstance(value, (str, int, float, bool)) and diagnostic.get(key) is None:
            diagnostic[key] = value
    return diagnostic


def raise_completion_workflow_failure(
    ticket_id: str,
    result: _ex.RunResult,
    failure_diagnostic: dict[str, Any] | None,
    workflow_steps_recorded: int,
    repo_root: str | None,
) -> NoReturn:
    """Finalize a failed completion workflow without widening dispatch policy."""

    diagnostic = dict(failure_diagnostic or {})
    diagnostic.setdefault("workflow_steps_recorded", workflow_steps_recorded)
    diagnostic.setdefault("workflow_status", result.status)
    if failure_diagnostic:
        from rebar.llm import usage_log
        from rebar.llm.gate_error_sidecar import emit_gate_error

        emit_gate_error(
            ticket_id,
            "completion",
            cause=result.error or "completion LLM tier failed",
            evidence_ref="completion-verification/recovery",
            diagnostic=diagnostic,
            repo_root=repo_root,
        )
        message = (
            result.error or "completion verification bounded recovery failed without a verdict"
        )
        # The primary run's repetition summary lands under aggregate_-prefixed
        # keys; format_repetition reads bare names, so project before rendering.
        repetition = {
            key.removeprefix("aggregate_"): value
            for key, value in diagnostic.items()
            if key.startswith("aggregate_")
        }
        if all(
            repetition.get(field) is not None
            for field in (
                "requests",
                "tool_calls",
                "tool_calls_distinct",
                "max_consecutive_repeat",
                "top_repeated_tool_calls",
            )
        ):
            # distinct_ratio_window is None BY DESIGN below REPETITION_WINDOW
            # tool calls; render a placeholder rather than dropping the line.
            if repetition.get("distinct_ratio_window") is None:
                repetition["distinct_ratio_window"] = "n/a(<window)"
            message = f"{message}\n{usage_log.format_repetition(repetition)}"
        raise CompletionRecoveryError(
            message,
            diagnostic=diagnostic,
        )
    raise LLMError(
        "completion verification workflow did not produce a verdict: "
        f"{result.error or 'LLM tier failed'}"
    )
