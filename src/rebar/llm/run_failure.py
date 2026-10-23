"""Classify and report structured-run failures for PydanticAIRunner.

This leaf owns interpret_failure, its FailureContext, and best-effort raw-reply
capture after bounded retries. structured_run owns the execution mechanism and
imports this failure classifier in one direction. Provider imports remain local
to functions, and this module does not import runner at runtime."""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, NoReturn

from rebar.llm import usage_log
from rebar.llm.errors import (
    LLMBudgetExhaustedError,
    LLMError,
    LLMInputRejectedError,
    LLMUnavailableError,
    RunawayToolLoopError,
)

if TYPE_CHECKING:  # import-only: `failure` stays a function-local import at runtime
    from rebar.llm.failure import ResolutionClass

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FailureContext:
    """The bounded, per-call facts ``interpret_failure`` needs to enrich/classify a raised
    exception (ADR 0056 decision 3) — everything the three ``except`` arms of
    ``PydanticAIRunner.run()`` used to close over as locals, threaded explicitly instead."""

    call_label: str
    execution_mode: str
    ran_model: str
    req_limit: int
    eff_max_iter: int
    started_at: float  # time.monotonic() at call start; elapsed computed inside


def interpret_failure(exc: BaseException, run_messages: list, ctx: FailureContext) -> NoReturn:
    """Classify a structured-run failure and always raise it.

    Handle UsageLimitExceeded before provider failures. Preserve
    RunawayToolLoopError's repetition details before the broad LLMError arm.
    Enrich an existing LLMError and raise the same object. For other exceptions,
    translate a rejected sampling parameter first. Otherwise classify the
    disposition. CHANGE_INPUT raises LLMInputRejectedError, while other classes
    raise LLMUnavailableError.
    """
    from pydantic_ai.exceptions import UsageLimitExceeded

    tool_calls_limit = max(8, ctx.eff_max_iter)
    if isinstance(exc, UsageLimitExceeded):
        # Computed BEFORE the log line so the repetition summary can be reported alongside the
        # budget numbers — a runaway that burned its budget on one repeated tool call reads very
        # differently from one that made steady progress.
        budget_diag = usage_log.run_shape(
            run_messages,
            request_limit=ctx.req_limit,
            tool_calls_limit=tool_calls_limit,
        )
        logger.warning(
            "llm call [%s] mode=%s model=%s hit step budget "
            "(request_limit=%d max_iterations=%d) in %.1fs %s",
            ctx.call_label,
            ctx.execution_mode,
            ctx.ran_model,
            ctx.req_limit,
            ctx.eff_max_iter,
            time.monotonic() - ctx.started_at,
            usage_log.format_repetition(budget_diag),
        )
        budget_err = LLMBudgetExhaustedError(
            f"agent exceeded its step budget (max_iterations={ctx.eff_max_iter}; "
            "~1 model request per tool call). Raise REBAR_LLM_MAX_STEPS or narrow "
            "the task."
        )
        budget_err.diagnostic = budget_diag  # type: ignore[attr-defined]
        raise budget_err from exc
    if isinstance(exc, RunawayToolLoopError):
        # Merge the run-shape counters (requests/limits/tokens) UNDER the guard's
        # raise-time keys: those are the ground truth of what tripped, so they win on
        # conflict over the message-derived recomputation.
        merged: dict[str, Any] = {
            **usage_log.run_shape(
                run_messages, request_limit=ctx.req_limit, tool_calls_limit=tool_calls_limit
            ),
            **exc.diagnostic,
        }
        exc.diagnostic = merged
        logger.warning(
            "llm call [%s] mode=%s model=%s aborted a runaway tool-call loop in %.1fs %s",
            ctx.call_label,
            ctx.execution_mode,
            ctx.ran_model,
            time.monotonic() - ctx.started_at,
            usage_log.format_repetition(merged),
        )
        raise exc
    if isinstance(exc, LLMError):
        # Preserve the typed failure while attaching bounded counters from
        # the failed run (no prompt/tool content).
        exc.diagnostic = usage_log.run_shape(  # type: ignore[attr-defined]
            run_messages,
            request_limit=ctx.req_limit,
            tool_calls_limit=tool_calls_limit,
        )
        raise exc
    # Translate a rejected sampling parameter before generic classification.
    # Other provider failures become LLMUnavailableError unless the provider
    # answered and rejected the input, as _generic_failure_error checks.
    from rebar.llm.failure import translate_sampling_parameter_rejection

    sampling_err = translate_sampling_parameter_rejection(exc, ctx.ran_model)
    if sampling_err is not None:
        sampling_err.diagnostic = usage_log.run_shape(  # type: ignore[attr-defined]
            run_messages,
            request_limit=ctx.req_limit,
            tool_calls_limit=tool_calls_limit,
        )
        raise sampling_err from exc
    logger.warning(
        "llm call [%s] mode=%s model=%s FAILED in %.1fs: %s",
        ctx.call_label,
        ctx.execution_mode,
        ctx.ran_model,
        time.monotonic() - ctx.started_at,
        exc,
    )
    # The classified disposition is computed FIRST because it now SELECTS the raised type
    # (bug 43d4), not just decorates it. Kept total (classify_llm_failure never raises), so
    # enriching the error can't mask it.
    from rebar.llm.failure import ClassifyContext, classify_llm_failure

    outcome = classify_llm_failure(exc, ClassifyContext(model=ctx.ran_model))
    provider_err = _generic_failure_error(exc, outcome.resolution_class)
    provider_err.diagnostic = usage_log.run_shape(  # type: ignore[attr-defined]
        run_messages,
        request_limit=ctx.req_limit,
        tool_calls_limit=tool_calls_limit,
    )
    # `.outcome` is attached to BOTH types (story civilized-immediate-mamba): 25+ sites read
    # the disposition off the raised OBJECT rather than off its type.
    provider_err.outcome = outcome  # type: ignore[attr-defined]
    raise provider_err from exc


def _generic_failure_error(exc: BaseException, resolution: ResolutionClass) -> LLMError:
    """Choose the broad failure exception from the classifier's resolution class.

    CHANGE_INPUT means the provider answered and rejected the input, so it raises
    LLMInputRejectedError. Every other class raises LLMUnavailableError. Avoid
    context-limit terms in the added prefix because sizing scans the whole error
    message. Keep the provider's own text intact so a provider-reported context-limit
    response still drives model escalation.
    """
    from rebar.llm.failure import ResolutionClass

    if resolution is ResolutionClass.CHANGE_INPUT:
        return LLMInputRejectedError(f"the LLM provider rejected the request input: {exc}")
    return LLMUnavailableError(f"the LLM provider call failed: {exc}")


def _write_parse_failure_artifact(
    artifact_dir: str, *, reply: str, model: str, contract: str, attempts: int
) -> str | None:
    """Best-effort capture of the raw model reply on FINAL structured-parse failure (story
    2fd6). Writes ONE JSON artifact into ``artifact_dir`` and returns its path, then rotates the
    directory to the newest 20 ``*.json`` files. ANY error (mkdir/permission/disk) is swallowed
    and ``None`` is returned — it MUST NEVER raise, so the caller's original parse error is never
    masked."""
    try:
        import json
        import uuid
        from datetime import datetime, timezone
        from pathlib import Path

        now = datetime.now(timezone.utc)
        d = Path(artifact_dir)
        d.mkdir(parents=True, exist_ok=True)
        stamp = now.strftime("%Y%m%dT%H%M%S%f")
        path = d / f"parse-failure-{stamp}-{uuid.uuid4().hex[:8]}.json"
        path.write_text(
            json.dumps(
                {
                    "reply": reply,
                    "model": str(model),
                    "contract": str(contract),
                    "attempts": int(attempts),
                    "timestamp": now.isoformat(),
                }
            )
        )
        existing = sorted(d.glob("*.json"), key=lambda p: p.stat().st_mtime)
        for stale in existing[:-20]:
            stale.unlink()
        return str(path)
    except Exception:  # noqa: BLE001 — best-effort capture must never mask the parse error
        return None
