"""Pydantic AI bounded-operation seams for the PROMPTED structured-output path (RP-01 S2).

The manual attempt scheduler in :func:`rebar.llm.structured_run._pai_structured` (a
``for _ in range(output_retries + 1)`` loop issuing a fresh ``run_sync`` per attempt) is
replaced by ONE bounded Pydantic AI ``Agent`` run. That single run composes the S1
output-policy adapter (:mod:`rebar.llm.pai_output`, as ``output_type=TextOutput(fn)``) with
the two seams this module supplies. Neither seam selects a provider, projects usage for
telemetry, or persists anything — they only express two EXISTING deterministic authorities
(:mod:`rebar.llm.structured` and :func:`rebar.llm.model_classes.own_window_tokens`) through
documented Pydantic AI hooks.

Seam 1 — wire-context projection (:func:`wire_history_processor`)
================================================================
A ``ProcessHistory`` capability whose ``before_model_request`` runs a
``Callable[[list[ModelMessage]], list[ModelMessage]]`` and returns a wire COPY. It never
mutates stored state, so ``result.all_messages()`` keeps the full history regardless of what
the wire omits. Rule, applied to each FAILED output ``ModelResponse`` (a response carrying
text but no tool call — a tool-calling turn is part of the agentic loop and is always kept):

* Carry it on the retry wire ONLY when, for every viable candidate model,
  ``response.usage.input_tokens + response.usage.output_tokens + effective_max_tokens``
  fits ``model_classes.own_window_tokens(candidate)`` — evaluated against the SMALLEST
  candidate window so a carried response fits every candidate the fallback chain may route to.
* Otherwise OMIT the response WHOLE (never partially). Missing / zero usage → omit (its size
  cannot be proven, so it never rides the wire).
* If the authoritative input ALONE plus the output reserve overflows
  (``input_tokens + effective_max_tokens > window``, i.e. nothing can be added and it still
  does not fit), raise :class:`rebar.llm.errors.ContextWindowExceededError` — rebar fails
  closed rather than SHORTENING authoritative system / user / tool content.
* The REQUEST side of the same rule (story f79d): every wire's ``ModelRequest`` content
  (instructions + part content) is estimated locally at :data:`CHARS_PER_TOKEN` chars/token,
  and ``estimate + effective_max_tokens > window`` raises the same
  :class:`ContextWindowExceededError` BEFORE the provider call. This is what covers the
  FIRST request — a history of ``[ModelRequest]`` carries no billed usage for the response
  rule to read, and previously went to the provider unmeasured (a 400 surfaced as
  ``LLMUnavailableError``). Per-op input bounds (enrich, spec-scan, plan-review sizing,
  completion ceilings) remain the first line; this seam is the shared backstop.

Seam 2 — truncation reclassification (:func:`concision_guard`)
==============================================================
An ``after_model_request`` capability attached IN PLACE OF
:func:`rebar.llm.pai_output.guard_capability` on the PROMPTED branch only (the NativeOutput
branch keeps the terminal ``guard_capability`` — a fixed decoding grammar, not verbosity,
drives a native turn's size, so a truncated native turn stays TERMINAL). Because the shared
:mod:`rebar.llm.structured` classifiers only RAISE (never return a reason), the guard does its
own read-only two-layer read for the truncation subset ONLY — ``finish_reason`` in
{``length``, ``max_tokens``} OR the raw ``provider_details['finish_reason']`` in that subset
(mirroring :func:`rebar.llm.structured.check_response`'s two layers so a provider_details-only
truncation, pydantic-ai #5221, is caught):

* on a truncation signal it raises, before parsing,
  ``pydantic_ai.ModelRetry`` carrying the exact concision instruction (bounded, in-run);
* otherwise it delegates to the SAME S1 logic — :func:`rebar.llm.structured.check_response`:
  a terminal :class:`rebar.llm.errors.UnretryableOutputError` (refusal / content_filter)
  propagates, a retryable :class:`rebar.llm.errors.StructuredOutputError` (transient
  ``error``) is translated into ``ModelRetry``.

The shared ``structured.py`` tables are READ, never mutated.

Combined-allowance derivation (one value seeds two budgets)
===========================================================
The output-retry allowance ``N`` is ``structured.OUTPUT_RETRIES`` (2 by default), lowered by
``req.structured_retry_limit`` when the caller sets it (see
:func:`rebar.llm.structured_run.output_retry_allowance`). That ONE value seeds BOTH:

* the Agent's ``retries={"output": N}`` — the bound on the shared per-run output-retry counter
  that BOTH a capability-hook ``ModelRetry`` (the concision guard, seam 2) AND a
  ``TextOutput``-validator ``ModelRetry`` (the parse/validation failure) decrement; and
* the ADDEND on the constructed ``UsageLimits.request_limit`` in
  :func:`rebar.llm.structured_run.build_usage_limits` (base ``ceil(eff_max_iter/2)`` PLUS
  ``N``, while the RETURNED ``req_limit`` stays the bare base for telemetry and the
  ``completion_banking`` ``2*B`` inverse).

Seeding both from ONE value guarantees the shared request budget can NEVER trip before the
output-retry counter, so an invalid-then-valid recovery always spends its retries on OUTPUT
correction rather than aborting on a request-budget stop. On exhaustion pydantic-ai raises
``UnexpectedModelBehavior("Exceeded maximum output retries (N)")``, which the operation
translates to a rebar-typed :class:`rebar.llm.errors.UnretryableOutputError`.

Pydantic AI symbols are imported lazily inside functions so top-level ``import rebar.llm``
stays stdlib-only (the ``structured_run`` / ``pai_output`` convention).
"""

from __future__ import annotations

from typing import Any

from rebar.llm.errors import (
    ContextWindowExceededError,
    StructuredOutputError,
    UnretryableOutputError,
)

# The concision reask handed back, in-run, on a PROMPTED truncation signal — an EXACT string
# (the limit-matrix oracle pins it). It tells the model to shrink its own output rather than
# aborting the step, so a merely-verbose over-cap turn recovers within the shared retry budget.
CONCISION_INSTRUCTION = (
    "Make your response as concise as possible while fulfilling the prompt and output "
    "format requirements."
)

# The truncation subset the PROMPTED guard reclassifies as a concision retry (a normalized
# ``length`` or the Anthropic-raw ``max_tokens``). Deliberately NOT the whole
# ``structured._UNRETRYABLE_STOP_REASONS`` set — refusal / content_filter stay terminal.
_TRUNCATION_REASONS = frozenset({"length", "max_tokens"})


def _response_is_truncated(response: Any) -> bool:
    """Two-layer read for the truncation subset ONLY, mirroring ``check_response``'s layers so a
    ``provider_details``-only truncation (#5221) is caught: the normalized ``finish_reason`` OR
    the raw ``provider_details['finish_reason']`` in {``length``, ``max_tokens``}."""
    if getattr(response, "finish_reason", None) in _TRUNCATION_REASONS:
        return True
    details = getattr(response, "provider_details", None)
    if isinstance(details, dict) and details.get("finish_reason") in _TRUNCATION_REASONS:
        return True
    return False


def concision_guard():
    """Return an ``AbstractCapability`` for the PROMPTED branch: reclassify a TRUNCATION as an
    in-run concision ``ModelRetry``, else delegate to the SAME S1 stop/refusal authority.

    A truncation signal (seam 2's two-layer read) raises ``ModelRetry`` with
    :data:`CONCISION_INSTRUCTION` BEFORE parsing. Otherwise :func:`structured.check_response`
    runs: a terminal :class:`UnretryableOutputError` (refusal / content_filter) propagates so
    the run aborts with no retry prompt; a retryable :class:`StructuredOutputError` (transient
    ``finish_reason='error'``) is translated into ``ModelRetry`` — preserving the original
    Rebar error as ``__cause__`` — so Pydantic AI drives its own bounded retry."""
    from pydantic_ai.capabilities import AbstractCapability

    class _ConcisionAwareGuard(AbstractCapability):
        async def after_model_request(self, ctx, *, request_context, response) -> Any:
            from pydantic_ai import ModelRetry

            from rebar.llm import structured

            if _response_is_truncated(response):
                raise ModelRetry(CONCISION_INSTRUCTION)
            try:
                structured.check_response(response)
            except UnretryableOutputError:
                raise
            except StructuredOutputError as err:
                raise ModelRetry(str(err)) from err
            return response

    return _ConcisionAwareGuard()


def _response_output_tokens(response: Any) -> tuple[int, int] | None:
    """``(input_tokens, output_tokens)`` for a response with PROVABLE size, else ``None``.

    Missing usage — or a non-positive ``input_tokens`` (zero/absent, the "unknown count"
    case) — returns ``None`` so the caller OMITS the whole response rather than guess its
    size onto the wire."""
    usage = getattr(response, "usage", None)
    if usage is None:
        return None
    inp = int(getattr(usage, "input_tokens", 0) or 0)
    out = int(getattr(usage, "output_tokens", 0) or 0)
    if inp <= 0:
        return None
    return inp, out


def _is_failed_output_response(response: Any) -> bool:
    """True for a text-only ``ModelResponse`` (a candidate failed OUTPUT turn).

    A response carrying ANY tool call is part of the agentic loop — dropping it would orphan
    the paired tool return on the next request — so it is never a projection candidate. Only a
    response with text and no tool call is a failed-output turn the fit rule may omit."""
    from pydantic_ai.messages import TextPart, ToolCallPart

    has_text = False
    for part in getattr(response, "parts", []):
        if isinstance(part, ToolCallPart):
            return False
        if isinstance(part, TextPart):
            has_text = True
    return has_text


def _wire_keeps_response(response: Any, reserve: int, window: int) -> bool:
    """Fit decision for one failed output response — include / omit / fail-closed.

    Include only when ``input_tokens + output_tokens + reserve <= window``. Omit when usage is
    unknown or the response's own output pushes it over. Raise
    :class:`ContextWindowExceededError` when the authoritative input ALONE plus the reserve
    overflows (nothing can be added and it still does not fit) — rebar never shortens
    authoritative input to make it fit."""
    tokens = _response_output_tokens(response)
    if tokens is None:
        return False
    inp, out = tokens
    if inp + reserve > window:
        raise ContextWindowExceededError(
            f"the next request's authoritative input (~{inp} tokens) plus the output reserve "
            f"({reserve} tokens) exceeds the smallest viable candidate model's context window "
            f"({window} tokens) — refusing to shorten authoritative input (RP-01 S2)"
        )
    return inp + out + reserve <= window


# Pre-flight estimator calibration for REQUEST content (story f79d): 4 chars/token, matching
# `plan_review.det_floor.CHARS_PER_TOKEN` and the inline `len//4 + 1` estimators. Deliberately
# permissive (prose/code average ≤4 chars/token) so calls that succeed today are not newly
# refused, while gross overflows (an input several times the window) are caught before the
# provider call. chars/2 would score enrich's maximal `window * 2`-char first-line bound at
# exactly `window` tokens and wrongly refuse it; chars/4 scores it at window/2 and admits it.
CHARS_PER_TOKEN = 4


def _request_text_chars(msg: Any) -> int:
    """Character count of one ``ModelRequest``'s authoritative content: ``instructions`` plus
    every part's ``content``, stringifying non-text content conservatively."""
    total = len(str(getattr(msg, "instructions", None) or ""))
    for part in getattr(msg, "parts", []) or []:
        content = getattr(part, "content", None)
        if content is None:
            continue
        total += len(content) if isinstance(content, str) else len(str(content))
    return total


def _estimate_request_tokens(messages: Any) -> int:
    """Estimated token count of the wire's REQUEST content (every ``ModelRequest``), at
    :data:`CHARS_PER_TOKEN` chars/token. Zero when the wire carries no request content."""
    from pydantic_ai.messages import ModelRequest

    chars = sum(_request_text_chars(m) for m in messages if isinstance(m, ModelRequest))
    return chars // CHARS_PER_TOKEN + (1 if chars else 0)


def _check_request_fit(messages: Any, reserve: int, window: int) -> None:
    """Fail closed when the wire's authoritative REQUEST content alone cannot fit.

    The request side of the fit rule (story f79d): the carried-response rule reads the
    provider's already-billed usage, which a first request does not have, so the request
    content is measured locally instead. Raises :class:`ContextWindowExceededError` when the
    estimate plus the output reserve overflows the smallest viable candidate window — rebar
    never shortens authoritative input to make it fit."""
    estimate = _estimate_request_tokens(messages)
    if estimate and estimate + reserve > window:
        raise ContextWindowExceededError(
            f"the request's authoritative input (~{estimate} estimated tokens at "
            f"{CHARS_PER_TOKEN} chars/token) plus the output reserve ({reserve} tokens) "
            f"exceeds the smallest viable candidate model's context window ({window} tokens) "
            "— refusing to shorten authoritative input (RP-01 S2 / story f79d)"
        )


def _make_wire_projection(candidates, effective_max_tokens):
    """Build the sync ``proc(messages) -> list`` history-processor callable.

    ``window`` is the SMALLEST candidate's own context window so a carried response fits every
    candidate the fallback chain may route to; ``reserve`` is the output-token budget held back
    for the next turn's answer."""
    from rebar.llm import model_classes

    reserve = int(effective_max_tokens or 0)
    windows = [model_classes.own_window_tokens(c) for c in (candidates or [None])]
    window = min(windows)

    def proc(messages):
        from pydantic_ai.messages import ModelResponse

        kept = []
        for msg in messages:
            if isinstance(msg, ModelResponse) and _is_failed_output_response(msg):
                if not _wire_keeps_response(msg, reserve, window):
                    continue
            kept.append(msg)
        # Story f79d: the request side of the fit rule — measured on EVERY wire, so the FIRST
        # request (history `[ModelRequest]`, which the response rule structurally never sees)
        # is checked exactly like every subsequent one.
        _check_request_fit(kept, reserve, window)
        return kept

    return proc


def wire_history_processor(candidates, effective_max_tokens):
    """Return the ``ProcessHistory`` capability projecting the retry wire (seam 1).

    ``candidates`` is the viable candidate-model set (the fallback chain, or the single
    resolved model); ``effective_max_tokens`` is the per-call output reserve. The returned
    capability runs :func:`_make_wire_projection`'s callable before each request, returning a
    filtered COPY that never mutates stored history."""
    from pydantic_ai.capabilities import ProcessHistory

    return ProcessHistory(processor=_make_wire_projection(candidates, effective_max_tokens))
