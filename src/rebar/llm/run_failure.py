"""Failure INTERPRETATION for the pydantic-ai structured-run seam — the ``except`` spine of
``PydanticAIRunner.run()`` (``interpret_failure`` + its ``FailureContext`` carrier) plus the
best-effort raw-reply capture written when the bounded structured-retry is exhausted.

Split out of ``structured_run.py`` (task solitary-burly-acouchi) along the existing
call-graph seam: the run MECHANISM lives there, how a failure is CLASSIFIED and reported
lives here. The dependency direction is one-way — ``structured_run`` imports this module,
never the reverse — so future failure-classification logic has an obvious small home instead
of re-inflating the orchestrator toward the 800-LOC cap.

Leaf module, same convention as ``structured_run``: nothing is imported from ``runner`` at
runtime, and the heavy provider libraries stay behind function-local imports so
``import rebar.llm`` remains stdlib-only.
"""

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
    """The ``except`` spine of ``PydanticAIRunner.run()`` (ADR 0056 decision 3), lifted out
    verbatim as a plain function. ALWAYS raises — never returns.

    Dispatches on the exception type via ``isinstance``, in exactly this order (the order IS
    the contract — see the ADR and the module-level test):

    1. ``UsageLimitExceeded`` — rebar's own step-budget stop, not a provider outage.
    2. ``RunawayToolLoopError`` — the in-flight loop breaker (bug c827); rebar aborting
       its own repeating run. A special case BEFORE the broad ``LLMError`` arm, whose
       blanket diagnostic overwrite would lose the guard's raise-time repetition keys —
       and never the provider-outage sweep.
    3. ``LLMError`` — already typed; enrich in place and re-raise the SAME object.
    4. anything else — try the sampling-parameter-rejection translation FIRST (a provider
       rejecting e.g. ``temperature`` must fail loudly/actionably, not be swept into the
       broad provider-outage bucket below); only if that returns ``None`` does the generic
       path run. That generic path raises :class:`LLMInputRejectedError` when — and only
       when — the classifier's ``resolution_class`` is ``CHANGE_INPUT`` (the provider
       ANSWERED and rejected the input: a context-length 400, a 413, a content-policy
       refusal), and :class:`LLMUnavailableError` for every other disposition.
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
    # A SYSTEMIC provider failure (auth / missing key / connection / rate-limit). Unify
    # into the provider-agnostic LLMUnavailableError so every prompt-using client gets ONE
    # recognizable "LLM couldn't run" signal — never a swallowed empty result
    # (fuel-posse-ball). The ONE exception is a provider that ANSWERED and rejected the
    # INPUT: see `_generic_failure_error` (bug 43d4).
    # Tried FIRST (story S3/2932): a provider rejecting a sampling parameter (e.g. Bedrock's
    # "temperature is deprecated for this model" on a model NOT in the capabilities.py
    # denylist) must fail LOUDLY and ACTIONABLY, not be misclassified as an opaque outage by
    # the broad LLMUnavailableError path below. Only when this returns None (not a
    # sampling-parameter rejection) does the existing path run, unchanged.
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
    """Pick the exception TYPE for the broad arm of :func:`interpret_failure` from the
    disposition the classifier already computed (bug 43d4).

    ``CHANGE_INPUT`` means the provider ANSWERED and rejected the INPUT (an oversized
    prompt — a context-length 400 or a 413 — or a content-policy refusal): deterministic and
    caller-fixable, so it must NOT masquerade as an outage. EVERY other disposition (5xx,
    529, 429, auth/``CHANGE_SETTINGS``, ``NEEDS_INVESTIGATION``) keeps raising
    :class:`LLMUnavailableError` verbatim — the key is the resolution CLASS, never "4xx" or
    "not retryable".

    The CHANGE_INPUT prefix deliberately contains NONE of the substrings
    ``plan_review.sizing.is_context_limit_error`` matches ("context", "token limit",
    "input length", "exceeds the maximum", …): that predicate tests the WHOLE message, so a
    prefix carrying one would make every rejected input — a content-filter refusal
    included — read as a context limit and burn the model-escalation ladder before emitting a
    bogus "too big to review" finding. ``{exc}`` rides through VERBATIM, so a real
    context-limit 400 still matches the predicate through the PROVIDER's own words and the
    size ladder finally engages where it was designed to.
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
