"""The structured-output execution cluster — a LEAF module split out of ``runner.py``
(task 2682) purely to buy back LOC headroom against the 800-line module-size cap.

``runner.py`` sat at exactly 800 LOC (the repo's hard cap in
``.github/module-size-limit.txt``), and two queued provider-seam stories need to add
lines to it. This module holds the bounded structured-output retry driver
(``_pai_structured``) and its small support cluster — usage extraction, the
zeroed-usage telemetry warning, and the per-request iteration/token-budget
helpers — verbatim, with no behaviour change.

Leaf module (the ``anthropic_model.py`` / ``capabilities.py`` convention): this module
imports NOTHING from ``runner`` at runtime — the one annotation that names
``RunRequest`` is deferred via ``from __future__ import annotations`` +
``TYPE_CHECKING``, so ``import rebar.llm`` stays stdlib-only and the dependency
direction is one-way (``runner`` -> ``structured_run``, never back).
"""

from __future__ import annotations

import logging
import math
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, NoReturn

from rebar.llm import usage_log
from rebar.llm.capabilities import CACHE_MIN_PREFIX_TOKENS, ModelCapabilities
from rebar.llm.errors import (
    LLMConfigError,
    LLMError,
    LLMRunnerError,
    LLMUnavailableError,
    StructuredOutputError,
    UnretryableOutputError,
)

if TYPE_CHECKING:
    from rebar.llm.config import LLMConfig
    from rebar.llm.runner import RunRequest

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
    2. ``LLMError`` — already typed; enrich in place and re-raise the SAME object.
    3. anything else — try the sampling-parameter-rejection translation FIRST (a provider
       rejecting e.g. ``temperature`` must fail loudly/actionably, not be swept into the
       broad provider-outage bucket below); only if that returns ``None`` does the generic
       ``LLMUnavailableError`` path run.
    """
    from pydantic_ai.exceptions import UsageLimitExceeded

    tool_calls_limit = max(8, ctx.eff_max_iter)
    if isinstance(exc, UsageLimitExceeded):
        # Computed BEFORE the log line so the repetition summary can be reported alongside the
        # budget numbers — a runaway that burned its budget on one repeated tool call reads very
        # differently from one that made steady progress.
        budget_diag = usage_log.failure_usage(
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
        budget_err = LLMRunnerError(
            f"agent exceeded its step budget (max_iterations={ctx.eff_max_iter}; "
            "~1 model request per tool call). Raise REBAR_LLM_MAX_STEPS or narrow "
            "the task."
        )
        budget_err.diagnostic = budget_diag  # type: ignore[attr-defined]
        raise budget_err from exc
    if isinstance(exc, LLMError):
        # Preserve the typed failure while attaching bounded counters from
        # the failed run (no prompt/tool content).
        exc.diagnostic = usage_log.failure_usage(  # type: ignore[attr-defined]
            run_messages,
            request_limit=ctx.req_limit,
            tool_calls_limit=tool_calls_limit,
        )
        raise exc
    # A SYSTEMIC provider failure (auth / missing key / connection / rate-limit). Unify
    # into the provider-agnostic LLMUnavailableError so every prompt-using client gets ONE
    # recognizable "LLM couldn't run" signal — never a swallowed empty result
    # (fuel-posse-ball).
    # Tried FIRST (story S3/2932): a provider rejecting a sampling parameter (e.g. Bedrock's
    # "temperature is deprecated for this model" on a model NOT in the capabilities.py
    # denylist) must fail LOUDLY and ACTIONABLY, not be misclassified as an opaque outage by
    # the broad LLMUnavailableError path below. Only when this returns None (not a
    # sampling-parameter rejection) does the existing path run, unchanged.
    from rebar.llm.failure import translate_sampling_parameter_rejection

    sampling_err = translate_sampling_parameter_rejection(exc, ctx.ran_model)
    if sampling_err is not None:
        sampling_err.diagnostic = usage_log.failure_usage(  # type: ignore[attr-defined]
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
    provider_err = LLMUnavailableError(f"the LLM provider call failed: {exc}")
    provider_err.diagnostic = usage_log.failure_usage(  # type: ignore[attr-defined]
        run_messages,
        request_limit=ctx.req_limit,
        tool_calls_limit=tool_calls_limit,
    )
    # Attach the classified disposition as METADATA (story civilized-immediate-mamba). This
    # does NOT change the raised type — every existing `except LLMUnavailableError` still
    # catches, and the per-seam wiring + exit-code use is story blackbear's. Kept total
    # (classify_llm_failure never raises), so enriching the error can't mask it.
    from rebar.llm.failure import ClassifyContext, classify_llm_failure

    provider_err.outcome = classify_llm_failure(  # type: ignore[attr-defined]
        exc, ClassifyContext(model=ctx.ran_model)
    )
    raise provider_err from exc


# Max chars of the model's faulty prior reply echoed back in the bounded-retry reask
# (story drake) — enough to diff a near-miss, bounded so a huge blob can't balloon the prompt.
_FAULTY_OUTPUT_SNIPPET_CHARS = 2000


def _pai_structured(Agent, model, caps, req: RunRequest, kwargs: dict, usage_limits):
    """Obtain a validated structured object via the reliability stack (1268).

    NATIVE path: where the provider enforces a strict json_schema (output_mode ->
    NativeOutput), Pydantic AI does constrained decoding + validation + the bounded
    retry — no json-repair needed. PROMPTED path (everyone else, incl. Anthropic):
    generate FREE TEXT, then run the DETERMINISTIC tolerant parse (json-repair) +
    Pydantic validators, with a single bounded retry that feeds the validation error
    back to the SAME model (NOT a second interpreter LLM). Returns
    ``(validated_model_instance, usage_dict)`` — the usage of the run that produced the
    accepted output (story 0250 cache-token observability).

    ``caps`` is resolved ONCE by the caller (``run()``) and threaded through rather than
    re-derived here — see its ``caps =`` assignment for why a real run reads the model
    OBJECT's profile but a ``model_override`` run reads the config-resolved STRING instead."""
    from pydantic_ai import NativeOutput

    from rebar.llm import contracts, structured

    model_cls = contracts.response_model_for(req.output_schema)
    mode_obj = structured.output_mode(model_cls, caps, thinking=req.thinking)
    if isinstance(mode_obj, NativeOutput):
        agent = Agent(
            model, output_type=mode_obj, retries={"output": structured.OUTPUT_RETRIES}, **kwargs
        )
        with usage_log.capture_attempt_messages():
            run_result = agent.run_sync(req.instructions, usage_limits=usage_limits)
        # Silent-success parity (story drake): the PromptedOutput path below already checks
        # the stop reason; the NativeOutput path previously returned output DIRECTLY, so a
        # truncated/refused NativeOutput turn was returned as a hollow verdict. Run the same
        # check here — a length/max_tokens/content_filter/refusal finish_reason raises
        # UnretryableOutputError → the gate degrades to INDETERMINATE, never a hollow PASS.
        structured.check_response(run_result.response)
        return run_result.output, _extract_usage(run_result)

    # PromptedOutput case: free-text + deterministic parse/validate + bounded retry. The
    # schema directive is appended so the model knows the EXACT output keys (the json-repair
    # path generates free text, so — unlike NativeOutput/PromptedOutput-as-output_type — the
    # schema is not otherwise conveyed; without it the model guesses keys and tolerant parsing
    # drops them to an empty object).
    agent = Agent(model, **kwargs)  # free text (output_type defaults to str)
    schema_hint = structured.schema_directive(model_cls)
    prompt = f"{req.instructions}\n\n{schema_hint}"
    last: Exception | None = None
    for _ in range(structured.OUTPUT_RETRIES + 1):
        with usage_log.capture_attempt_messages():
            result = agent.run_sync(prompt, usage_limits=usage_limits)
        try:
            # A refused / TRUNCATED turn is surfaced as a clear error BEFORE the tolerant
            # parse — else json-repair would "fix" a truncated fragment into a
            # plausible-but-wrong object (the false-accept the stop-reason guard prevents).
            structured.check_response(result.response)
            parsed = structured.parse_structured(str(result.output), model_cls)
            return parsed, _extract_usage(result)
        except UnretryableOutputError:
            # A truncation (hit the output cap), refusal, or content-filter is a complete,
            # unusable turn — re-running the same call reproduces it. FAST-FAIL instead of
            # re-paying this (often agentic, multi-minute) call OUTPUT_RETRIES more times.
            raise
        except StructuredOutputError as exc:
            last = exc
            # Feed the model its OWN faulty prior reply (bounded) so it can diff its mistake
            # — the LangChain RetryWithErrorOutputParser / Instructor pattern (story drake).
            faulty = str(result.output)
            if len(faulty) > _FAULTY_OUTPUT_SNIPPET_CHARS:
                faulty = faulty[:_FAULTY_OUTPUT_SNIPPET_CHARS] + " …[truncated]"
            prompt = (
                f"{req.instructions}\n\n{schema_hint}\n\nYour previous reply could not be "
                f"parsed/validated ({exc}). Your previous reply was:\n{faulty}\n\n"
                f"Reply with ONLY the JSON object matching the schema above — no prose, "
                f"no code fence."
            )
    assert last is not None  # the loop only exits here after a failed parse set `last`
    raise last  # exhausted the bounded retry; surface the last validation error


def build_model_settings(
    cfg: LLMConfig,
    req: RunRequest,
    caps: ModelCapabilities,
    resolved: str,
    cache_settings: dict | None,
    *,
    model_override,
) -> dict:
    """Assemble the ``model_settings`` dict for this call (ADR 0056 decision 3), lifted out of
    ``PydanticAIRunner.run()`` as a PURE function — no logging, no mutation of any module-level
    state. ``model_override`` is accepted only to document/preserve the invariant that
    ``cache_settings`` was ALREADY computed by the caller as
    ``None if model_override else cache_settings_for(caps, execution_mode=req.execution_mode)``
    (see ``run()``'s own assignment) —
    it is never recomputed here, so the guard that keeps the offline ``TestModel`` path free of
    cache flags cannot be silently dropped by this move.

    Sampling temperature (upstream review-code report §2): a per-request override on
    ``req.config`` wins over the runner's own ``cfg``, mirroring the ``max_tokens`` seam below;
    both are ``None`` by default so the provider default is used (byte-unchanged). The Pass-2
    verify steps carry ``temperature=0`` (greedy) so re-running the same finding does not
    resample its narrow verification and flip a block/advisory decision. `temperature` is
    WITHDRAWN (never sent) for a model MEASURED to reject it (story S3/2932: e.g.
    ``us.anthropic.claude-opus-4-7`` 400s "temperature is deprecated for this model") — ``caps``
    already carries this per-model fact, so this is a capability check, never a provider-name
    branch. The once-per-model dedup INFO log for that withdrawal stays in ``run()`` (this
    function must stay side-effect-free; see ``_TEMPERATURE_WITHDRAWN_LOGGED`` in ``runner.py``
    and the module-level purity test in ``test_structured_run_seam.py``)."""
    del model_override  # documented above: only `cache_settings` (already gated) is consumed
    # Wire the configured OUTPUT cap into the call. cfg.max_tokens was previously DROPPED
    # (only the cache flags were sent as model_settings), so pydantic-ai fell back to its
    # max_tokens=4096 default — far too small for a multi-child container review, whose
    # output truncated (stop_reason=max_tokens) and tripped the structured-output retry;
    # max_tokens is a base ModelSettings field, riding alongside the cache flags.
    model_settings = dict(cache_settings) if cache_settings is not None else {}
    # The output cap is PER-REQUEST too (bug spy-luge-wool / sole-teal-churn): a finding-rich
    # Pass-2 verifier carries a scaled max_tokens on ``req.config`` so its structured output
    # doesn't truncate (finish_reason=length), without mutating a shared runner's self._config.
    # A request can only RAISE the configured floor, never lower it.
    eff_max_tokens = effective_max_tokens(cfg.max_tokens, getattr(req.config, "max_tokens", None))
    # Clamp to the RESOLVED model's published output ceiling (bug 1019-e1e9-5117-4795). The cap
    # above is monotonically RAISED — `max_output_cfg` lifts it to the ceiling of whichever model
    # was resolved at the time and never lowers it again — so a cap raised for a 128K verifier
    # rides into a later call that resolves to the 64K trivial model. Anthropic's API tolerated
    # the oversized ask; Bedrock rejects it outright with
    # `ValidationException: The maximum tokens you requested exceeds the model limit`, and the
    # call never runs. Asking a model for more output than it accepts cannot produce a larger
    # answer — only a guaranteed failure — so clamping here loses nothing.
    #
    # Applied ONLY to a model the ceiling table actually knows. `model_max_output_tokens` falls
    # back to the configured default (16000) for an unmapped model, and clamping to THAT would
    # silently shrink a deliberate operator floor on any model outside the gate ladder — a
    # regression worse than the bug. An unmapped model keeps today's behaviour.
    _ceiling = _known_model_output_ceiling(resolved)
    if _ceiling is not None:
        eff_max_tokens = min(eff_max_tokens, _ceiling)
    if req.output_token_limit is not None:
        eff_max_tokens = min(eff_max_tokens, max(256, int(req.output_token_limit)))
    if eff_max_tokens:
        model_settings["max_tokens"] = eff_max_tokens
    # Wire the configured wall-clock timeout so the operator's REBAR_LLM_TIMEOUT
    # actually bounds each LLM call. Audit reliability #6: cfg.timeout_s was resolved
    # into LLMConfig but never passed to the model, so every call silently rode the
    # Anthropic SDK's ~600 s default regardless of the operator's setting. `timeout`
    # is a base ModelSettings field mapping to the underlying httpx/Anthropic client
    # request timeout. DEFAULT_TIMEOUT_S (600) equals the SDK default, so an unset
    # knob is never lowered below it; an explicit operator value is honored verbatim.
    if cfg.timeout_s:
        model_settings["timeout"] = float(cfg.timeout_s)
    temperature = getattr(req.config, "temperature", None)
    if temperature is None:
        temperature = cfg.temperature
    if temperature is not None and caps.supports_temperature:
        model_settings["temperature"] = float(temperature)
    return model_settings


def build_usage_limits(cfg: LLMConfig, req: RunRequest, UsageLimits) -> tuple[Any, int, int]:
    """Compute the per-call step budget (ADR 0056 decision 3), lifted out of
    ``PydanticAIRunner.run()`` verbatim. ``UsageLimits`` is threaded in as a parameter (rather
    than imported here) because ``run()`` already imports it lazily from ``pydantic_ai.usage``
    (heavy libraries stay out of module top per this package's convention).

    pydantic-ai's ``request_limit`` counts MODEL REQUESTS (~1 per tool-call cycle). Halve
    ``cfg.max_iterations`` (authored as ~2 steps per tool-call cycle) so a given
    ``max_iterations`` allows the intended number of cycles (and we DON'T silently inherit
    pydantic-ai's default ``request_limit=50``). ``request_limit`` bounds model TURNS, not tool
    calls WITHIN a turn — a failing/re-called tool can spray many calls in few turns
    (pydantic-ai #2593); ``tool_calls_limit`` is the in-turn backstop so a failing/looping
    tool cannot burn the whole budget (the retry-to-exhaustion failure mode), set generously
    above the expected ``~max_iterations/2`` tool calls so it only trips on a genuine runaway.
    The step budget is PER-REQUEST: a caller may raise ``max_iterations`` for THIS call via
    ``req.config`` — needed so a finding-rich Pass-2 verifier gets a scaled budget without a
    shared runner's ``self._config`` changing under other steps (bug 59bc); it can only RAISE
    the floor, never lower it. ``self._config`` (``cfg``) is the floor; ``req.config`` is the
    per-call override.

    Returns ``(usage_limits, req_limit, eff_max_iter)`` — ``req_limit``/``eff_max_iter`` are
    handed back explicitly because neither is recoverable from the built ``UsageLimits``: only
    ``max(8, eff_max_iter)`` is stored there as ``tool_calls_limit``, and ``max()`` is not
    invertible for ``eff_max_iter <= 8``. ``run()`` needs both for ``FailureContext`` and the
    telemetry log."""
    eff_max_iter = effective_max_iterations(
        cfg.max_iterations, getattr(req.config, "max_iterations", None)
    )
    if req.iteration_limit is not None:
        eff_max_iter = min(eff_max_iter, max(2, int(req.iteration_limit)))
    # The model-REQUEST ceiling (~1 per tool-call cycle). Bound to a LOCAL so the telemetry
    # logs report it directly instead of reading it back off the UsageLimits object (which a
    # test may stub) — and so the step-usage line reports the EFFECTIVE per-request budget.
    req_limit = max(1, math.ceil(eff_max_iter / 2))
    usage_limits = UsageLimits(
        request_limit=req_limit,
        tool_calls_limit=max(8, eff_max_iter),
    )
    return usage_limits, req_limit, eff_max_iter


def effective_max_iterations(floor: int, requested: int | None) -> int:
    """The PER-REQUEST agent step budget (bug 59bc). A caller may RAISE the budget for a single
    call by carrying a higher ``max_iterations`` on its ``RunRequest.config`` (e.g. the Pass-2
    verifier scaled by its finding count), without mutating a shared runner's ``self._config``
    under other steps. The request can only raise the operator-configured floor, never lower it —
    so ``max(floor, requested)``; a missing/None request value leaves the floor untouched."""
    return max(floor, requested or floor)


def _known_model_output_ceiling(model: str | None) -> int | None:
    """The published output ceiling for ``model``, or ``None`` when the table does not know it.

    Deliberately NOT :func:`review_kernel.verify.model_max_output_tokens`, which answers the
    different question "what budget should a review ride at" and therefore substitutes the
    configured default for an unmapped model. Here an unmapped model must yield ``None`` (leave
    the cap alone) rather than a default that would clamp a legitimate operator floor downward.
    Imported lazily off the same single-source table so the two cannot drift.
    """
    if not model:
        return None
    from rebar.llm.review_kernel.verify import MODEL_MAX_OUTPUT_TOKENS

    for name, cap in MODEL_MAX_OUTPUT_TOKENS:
        if name in model:
            return cap
    return None


def effective_max_tokens(floor: int, requested: int | None) -> int:
    """The PER-REQUEST output-token cap (bug spy-luge-wool / sole-teal-churn) — the exact analogue
    of :func:`effective_max_iterations` for the per-call OUTPUT budget. A finding-rich Pass-2 verify
    emits ~1 verification object per finding, so its structured output overflows a fixed cap
    (``finish_reason=length``) and the whole review collapses to INDETERMINATE. A caller scales
    the cap for a single call via ``RunRequest.config.max_tokens``; it can only RAISE the operator
    floor, never lower it — ``max(floor, requested)`` — a missing/None request leaves it as-is."""
    return max(floor, requested or floor)


def _extract_usage(run_result) -> dict[str, int]:
    """Pull the per-run token usage off a pydantic-ai ``AgentRunResult`` (story 0250).

    Pins the pydantic-ai 1.107.0 ``RunUsage`` field names — note the library NORMALIZES
    Anthropic's raw ``cache_read_input_tokens`` / ``cache_creation_input_tokens`` to
    ``cache_read_tokens`` / ``cache_write_tokens`` (usage.py:194-200). Also reads
    ``requests`` — the model-REQUEST count for this run, the step-usage signal the
    ``max_iterations`` / ``request_limit`` budget bounds (so a run's headroom against the
    step floor is observable; used to size the verifier/reviewer floors from data rather
    than guesswork). Defensive: a missing ``.usage()`` (e.g. an injected test model) yields
    an empty dict, never an error — usage is observability, never load-bearing."""
    try:
        # pydantic-ai 1.107.0 deprecates the ``.usage()`` METHOD in favour of the
        # ``.usage`` PROPERTY (which exposes the token attrs directly). Read the
        # property's attrs — only fall back to CALLING it for a legacy build where
        # ``.usage`` is still a bare method (no attrs), so we never trip the
        # call-the-property deprecation warning on the supported version.
        u = run_result.usage
        if not hasattr(u, "input_tokens") and callable(u):
            u = u()
    except Exception:  # noqa: BLE001 — usage is best-effort observability, never fails a run
        return {}
    return {
        "input_tokens": int(getattr(u, "input_tokens", 0) or 0),
        "output_tokens": int(getattr(u, "output_tokens", 0) or 0),
        "cache_read_tokens": int(getattr(u, "cache_read_tokens", 0) or 0),
        "cache_write_tokens": int(getattr(u, "cache_write_tokens", 0) or 0),
        # The model-REQUEST count (~1 per agentic tool-call cycle). Surfaced so Pass-2
        # verify step usage is observable vs its budget (the agentic verifier's
        # step-budget headroom — bug 59bc); 0/absent for a single-turn call.
        "requests": int(getattr(u, "requests", 0) or 0),
    }


def warn_if_cache_ineffective(
    usage: dict,
    *,
    caching_requested: bool,
    model: str,
    marked_prefix_tokens: int | None = None,
    cache_min_prefix_tokens: int = CACHE_MIN_PREFIX_TOKENS,
) -> None:
    """Telemetry-only WARNING (never a block) when prompt caching was REQUESTED but reports
    ZERO effect (story S3/2932).

    MEASURED against real AWS: ``us.`` AND ``global.`` opus-4-5 both report cache_read=0 AND
    cache_write=0 while billing the FULL input tokens (4029 in the measured run) — no error,
    no warning from the provider. Caching is MODEL-dependent, not profile-prefix-dependent (a
    controlled same-model `us.` vs `global.` comparison proved the prefix is not the variable).
    Without this warning an operator silently pays full price on every call, forever, with no
    signal anything is wrong.

    This is DELIBERATELY a separate predicate from ``_warn_if_zeroed_usage`` above: that one
    fires on ``input_tokens == 0`` (a request that plausibly never happened), whereas here
    ``input_tokens`` is healthy/nonzero (a REAL request was billed) — the existing predicate
    would never fire for this case. An ineffective cache is a COST problem, not a correctness
    one, so this is WARNING-level observability only and never raises/blocks.

    Bounded BELOW by ``CACHE_MIN_PREFIX_TOKENS`` (bug 7a79). The claim being made is "a
    cacheable prompt silently failed to cache", which requires the prompt to have been
    cacheable: below the floor the anthropic cache never writes or reads, so zero/zero is the
    CORRECT reading and no configuration change could alter it. Unbounded, the predicate fired
    on every small call — ~20 lines per ``rebar review-plan`` run, on the same runs whose
    AGGREGATE usage reported ``cache_write_tokens > 0`` — which both contradicted the run's own
    telemetry and trained operators to filter out the one signal that catches real cost bleed.
    The floor makes the warning mean what it says; the above-floor detection is unchanged.

    Bug e3cd corrected BOTH halves of that bound.

    * ``cache_min_prefix_tokens`` is now the CALLER'S per-model floor (read off
      ``ModelCapabilities``), not a model-blind 4096. It defaults to the conservative global
      so an un-updated caller keeps today's exact behavior.
    * ``marked_prefix_tokens`` is the size of the MARKED PREFIX -- the bytes ahead of the
      ``cache_control`` breakpoint -- which is what actually governs whether the cache
      engages. The old predicate tested total ``input_tokens``, so a call with a 150-token
      marked prefix behind a 7000-token UNMARKED user message cleared the floor on totals
      while being uncacheable in fact. When it is None (a caller that cannot measure it) the
      pre-existing total-based predicate applies unchanged.

    With the marked prefix known there are two distinct, mutually exclusive reports, because
    they have different causes and therefore different remedies:

    A. marked prefix AT/ABOVE the floor, zero/zero -> the prompt WAS cacheable and the cache
       silently did not engage. The original story-S3 signal, retargeted.
    B. marked prefix BELOW the floor while a floor's worth of payload rides OUTSIDE the
       breakpoint -> the prompt can never cache as marked, and the changeable thing is where
       the breakpoint sits. Reporting the total here (as the old predicate did) named the
       wrong quantity and so implied the wrong remedy, since the prompt looks plenty big.

    Case B is deliberately bounded by ``unmarked >= floor`` rather than firing on every small
    marked prefix: the signal is "a cacheable-sized payload is riding outside the breakpoint",
    not "the prefix is small". Without that bound this would re-create the 7a79 spam."""
    if not (
        caching_requested
        and usage.get("cache_read_tokens", 0) == 0
        and usage.get("cache_write_tokens", 0) == 0
    ):
        return

    input_tokens = usage.get("input_tokens", 0)

    if marked_prefix_tokens is None:
        if input_tokens >= cache_min_prefix_tokens:
            logger.warning(
                "llm prompt caching requested for model=%s but had NO effect (cache_read=%s, "
                "cache_write=%s) despite input_tokens=%s - caching is model-dependent and can "
                "fail silently (no error from the provider); the operator is paying full "
                "input price on every call",
                model,
                usage.get("cache_read_tokens", 0),
                usage.get("cache_write_tokens", 0),
                input_tokens,
            )
        return

    if marked_prefix_tokens >= cache_min_prefix_tokens:
        logger.warning(
            "llm prompt caching requested for model=%s but had NO effect (cache_read=%s, "
            "cache_write=%s) despite a marked prefix of %s tokens, at/above this model's "
            "%s-token minimum - caching is model-dependent and can fail silently (no error "
            "from the provider); the operator is paying full input price on every call",
            model,
            usage.get("cache_read_tokens", 0),
            usage.get("cache_write_tokens", 0),
            marked_prefix_tokens,
            cache_min_prefix_tokens,
        )
        return

    if input_tokens - marked_prefix_tokens >= cache_min_prefix_tokens:
        logger.warning(
            "llm prompt caching requested for model=%s but CANNOT engage: only %s tokens sit "
            "ahead of the cache breakpoint, below this model's %s-token minimum, while %s of "
            "the %s billed input tokens ride AFTER it unmarked - the provider declines a "
            "sub-minimum prefix silently (no error, cache_read=%s cache_write=%s). The "
            "changeable thing is where the breakpoint sits, not the size of the prompt",
            model,
            marked_prefix_tokens,
            cache_min_prefix_tokens,
            input_tokens - marked_prefix_tokens,
            input_tokens,
            usage.get("cache_read_tokens", 0),
            usage.get("cache_write_tokens", 0),
        )


def cache_write_never_read(records: list[dict], *, min_calls: int = 2) -> bool:
    """True when a RUN's caching calls all WROTE the cache and NONE ever READ it (bug 1dbe).

    The per-call :func:`warn_if_cache_ineffective` cannot see this shape: each individual
    write>0/read==0 call is BENIGN in isolation (the first call of any warm-then-reuse
    sequence writes and reads nothing). The pathology is only visible ACROSS a run's calls —
    "every caching call paid the write PREMIUM and not one collected the read DISCOUNT" — which
    is exactly the state bug 1dbe measured (three plan-review passes each writing thousands of
    tokens, every read zero, on back-to-back runs). It stayed invisible because the only cache
    telemetry fired on write==0 AND read==0.

    Fires only with at least ``min_calls`` (default 2) CACHING calls — a call is "caching" here
    iff it wrote OR read a cache (``cache_write_tokens`` or ``cache_read_tokens`` present and
    nonzero). A single caching call is the legitimate first write and is never flagged; a run
    with no caching calls at all is out of scope (the write==0/read==0 predicate owns that).
    Returns True iff every caching call wrote (>0) and every caching call read exactly 0."""
    caching = [
        r
        for r in records
        if int(r.get("cache_write_tokens", 0) or 0) or int(r.get("cache_read_tokens", 0) or 0)
    ]
    if len(caching) < min_calls:
        return False
    return all(
        int(r.get("cache_write_tokens", 0) or 0) > 0
        and int(r.get("cache_read_tokens", 0) or 0) == 0
        for r in caching
    )


def warn_if_cache_write_never_read(records: list[dict], *, model: str = "?") -> None:
    """Telemetry-only WARNING (never a block) for the write-every-call-never-read run shape
    (bug 1dbe) — the aggregate companion to the per-call :func:`warn_if_cache_ineffective`.

    Called over a RUN's usage records (e.g. from :func:`rebar.llm.usage_log.summarize`). When
    :func:`cache_write_never_read` holds, the operator is paying the cache-WRITE premium on
    every call and collecting the read discount on none — pure loss — most often because the
    marked prefix varies per call (no breakpoint sits at the byte-identical shared segment).
    Observability only; a run that genuinely never re-uses a prefix is at worst a benign
    warning."""
    if not cache_write_never_read(records):
        return
    caching = [
        r
        for r in records
        if int(r.get("cache_write_tokens", 0) or 0) or int(r.get("cache_read_tokens", 0) or 0)
    ]
    total_write = sum(int(r.get("cache_write_tokens", 0) or 0) for r in caching)
    logger.warning(
        "llm prompt caching WROTE on every one of %s caching call(s) (model=%s) and was READ "
        "by NONE (%s cache_write tokens billed at premium, cache_read=0 across the run) - the "
        "marked prefix likely varies per call, so no breakpoint sits at the byte-identical "
        "shared segment; the write premium is pure loss until one does",
        len(caching),
        model,
        total_write,
    )


def estimate_marked_prefix_tokens(cache_settings: Any, *, system_prompt: str) -> int | None:
    """Estimated size of the bytes AHEAD of the cache breakpoint, or ``None`` if unknowable.

    ``warn_if_cache_ineffective`` needs the MARKED PREFIX, not the total input (bug e3cd), and
    the only component that can name it is the one that decided where the breakpoint goes.
    ``capabilities.cache_settings_for`` always sets the instructions + tool-definitions
    breakpoints, and pydantic-ai puts ``cache_control`` on the LAST SYSTEM BLOCK for the
    former, so the marked prefix is the system prompt. Bug dd27 added a THIRD, message-tail
    breakpoint on the multi-turn arm; it sits BEHIND the system prompt, so it can only enlarge
    the truly-cached span and never shrinks the prefix estimated here — the estimate stays
    conservative in the same direction as the two below.

    Two deliberate conservatisms, both erring toward NOT warning:

    * Tool definitions render AHEAD of the system prompt and are inside the marked span, but
      they are not sized here (their serialized JSON is not available at this seam). Omitting
      them UNDER-counts, which can only suppress a warning, never manufacture one. It is a
      no-op on rebar's single-turn calls, which send no tools.
    * Returning ``None`` when no instructions breakpoint is set routes the caller back to the
      pre-existing total-``input_tokens`` predicate rather than asserting a marked size that
      was never established.

    The chars/4 estimate matches ``plan_review.det_floor.est_tokens``, imported lazily to keep
    this leaf module free of a package that imports back into ``llm``."""
    if not cache_settings:
        return None
    if not (
        cache_settings.get("anthropic_cache_instructions")
        or cache_settings.get("bedrock_cache_instructions")
    ):
        return None
    from rebar.llm.plan_review.det_floor import est_tokens

    return est_tokens(system_prompt)


def _warn_if_zeroed_usage(usage: dict) -> None:
    """Telemetry-only WARNING (never a block) when a REAL run reports all-zero token usage
    despite having made a request — the #5360 zeroed-adapter signal. Observability, not
    load-bearing; a genuinely tiny run is at worst a benign warning."""
    if (
        usage.get("requests", 0) > 0
        and usage.get("input_tokens", 0) == 0
        and usage.get("output_tokens", 0) == 0
    ):
        logger.warning(
            "llm usage looks zeroed/implausible (requests=%s, input=0, output=0) — the "
            "provider adapter may be under-reporting usage",
            usage.get("requests"),
        )


# ── pydantic-ai import + config preflight ─────────────────────────────────────────────
# Relocated from `runner` (ticket 3a98) so the runner keeps real headroom under the
# module-size cap after main grew it. They live HERE because this module already owns the
# pydantic-ai plumbing (`_pai_structured`), so all `_pai_*` machinery stays together.
# `runner` re-imports both, which preserves them as ITS module-globals — the existing
# `monkeypatch.setattr(runner_mod, "_import_pydantic_ai", ...)` seams keep working unchanged.


def _import_pydantic_ai():
    try:
        from pydantic_ai import Agent
    except ImportError as exc:
        raise LLMConfigError(
            "the pydantic_ai runner needs the 'agents' extra (pydantic-ai-slim). "
            "Install it with: pip install 'nava-rebar[agents]'"
        ) from exc
    return Agent


def _pai_check_config(cfg: LLMConfig) -> None:
    """VALIDATE ``base_url``/``api_key`` rather than refuse them outright (story S4): this
    used to raise for ANY ``base_url``/``api_key``, making the OpenAI-compatible local-server
    recipe ``docs/llm-framework.md`` documents a false promise. ``rebar.llm.providers`` now
    builds a real provider for a configured ``base_url``; this only rejects ambiguous/
    malformed config, still LOUDLY: ``api_key`` WITHOUT ``base_url`` (direct-OpenAI instead
    reads the vendor SDK's own ``OPENAI_API_KEY``), or a ``base_url`` missing a scheme/host."""
    if cfg.api_key and not cfg.base_url:
        raise LLMConfigError(
            "REBAR_LLM_API_KEY (api_key) is set without base_url — ambiguous: the direct "
            "provider path reads the vendor SDK's own env var (e.g. OPENAI_API_KEY) instead. "
            "Set base_url too (an OpenAI-compatible endpoint) or unset api_key."
        )
    if cfg.base_url:
        from urllib.parse import urlparse

        parsed = urlparse(cfg.base_url)
        if not (parsed.scheme and parsed.netloc):
            raise LLMConfigError(
                f"base_url (REBAR_LLM_BASE_URL) is not an absolute URL: {cfg.base_url!r} — "
                "expected e.g. 'http://localhost:1234/v1'"
            )
