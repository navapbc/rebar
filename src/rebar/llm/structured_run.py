"""The structured-output execution cluster — a LEAF module split out of ``runner.py``
(task 2682) purely to buy back LOC headroom against the 800-line module-size cap.

``runner.py`` sat at exactly 800 LOC (the repo's hard cap in
``.github/module-size-limit.txt``), and two queued provider-seam stories need to add
lines to it. This module holds the bounded structured-output retry driver
(``_pai_structured``) and its small support cluster — usage extraction and the
per-request iteration/token-budget helpers — verbatim, with no behaviour change.

This module itself later reached the cap and was split the same way (task
solitary-burly-acouchi), by RESPONSIBILITY along the seams that already existed: the run
MECHANISM stayed here, cache-effectiveness DIAGNOSTICS moved to ``cache_diagnostics``, and
failure INTERPRETATION moved to ``run_failure``. Both are one-way dependencies (this module
imports them, never the reverse), so new diagnostics or failure-classification logic has its
own small home instead of re-inflating this one. Their public names are re-exported below so
every pre-split import path — ``from rebar.llm.structured_run import interpret_failure`` and
friends — keeps resolving unchanged.

Leaf module (the ``anthropic_model.py`` / ``capabilities.py`` convention): this module
imports NOTHING from ``runner`` at runtime — the one annotation that names
``RunRequest`` is deferred via ``from __future__ import annotations`` +
``TYPE_CHECKING``, so ``import rebar.llm`` stays stdlib-only and the dependency
direction is one-way (``runner`` -> ``structured_run``, never back).
"""

from __future__ import annotations

import logging
import math
from typing import TYPE_CHECKING, Any

from rebar.llm import usage_log
from rebar.llm.cache_diagnostics import (
    _warn_if_zeroed_usage,
    cache_write_never_read,
    estimate_marked_prefix_tokens,
    warn_if_cache_ineffective,
    warn_if_cache_write_never_read,
)
from rebar.llm.capabilities import ModelCapabilities
from rebar.llm.errors import (
    LLMConfigError,
    StructuredOutputError,
    UnretryableOutputError,
)
from rebar.llm.run_failure import (
    FailureContext,
    _write_parse_failure_artifact,
    interpret_failure,
)

if TYPE_CHECKING:
    from rebar.llm.config import LLMConfig
    from rebar.llm.runner import RunRequest

# Re-exported so the split is invisible to every caller: `runner.py`, `usage_report.py` and
# the existing test suite import these names FROM HERE, and `test_structured_run_seam.py`
# asserts `hasattr(structured_run, "_warn_if_zeroed_usage")`. Listing them in `__all__` is
# what marks them as deliberate re-exports (rather than unused imports) — the private two are
# included for the same compatibility reason, not because they are public API.
__all__ = [
    "FailureContext",
    "_extract_usage",
    "_pai_check_config",
    "_pai_structured",
    "_warn_if_zeroed_usage",
    "_write_parse_failure_artifact",
    "apply_structured_seams",
    "build_model_settings",
    "build_usage_limits",
    "cache_write_never_read",
    "effective_max_iterations",
    "effective_max_tokens",
    "estimate_marked_prefix_tokens",
    "interpret_failure",
    "output_retry_allowance",
    "warn_if_cache_ineffective",
    "warn_if_cache_write_never_read",
]

logger = logging.getLogger(__name__)


def _pai_structured(
    Agent,
    model,
    caps,
    req: RunRequest,
    kwargs: dict,
    usage_limits,
    *,
    artifact_dir: str | None = None,
):
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
    OBJECT's profile but a ``model_override`` run reads the config-resolved STRING instead.

    RP-01 S2: the manual per-attempt scheduler is replaced by ONE bounded Pydantic ``Agent``
    run. Both branches share the wire projection + one output-retry counter (already wired into
    ``kwargs`` as ``capabilities`` / ``retries`` by ``build_agent_kwargs``); this function only
    selects the output mode and attaches the PER-BRANCH guard."""
    from pydantic_ai import NativeOutput

    from rebar.llm import contracts, structured

    model_cls = contracts.response_model_for(req.output_schema)
    mode_obj = structured.output_mode(model_cls, caps, thinking=req.thinking)
    if isinstance(mode_obj, NativeOutput):
        # Bug 895c: the provider compiles this contract's JSON Schema into a decoding grammar
        # and can 400 outright ("Grammar compilation timed out." / "Schema is too complex.").
        # `structured.output_mode` already keeps measured-too-complex contracts off this path,
        # so reaching here means the bound under-predicted for THIS model/contract pair — fall
        # back to the PROMPTED path below (measured to return the same verdict in ~11s) rather
        # than losing this step to a request that can never succeed as configured.
        try:
            return _run_native_output(Agent, model, mode_obj, req, kwargs, usage_limits)
        except Exception as exc:
            from rebar.llm.failure import translate_schema_complexity_rejection

            ran_model = getattr(model, "model_name", None) or str(model)
            if translate_schema_complexity_rejection(exc, ran_model) is None:
                raise
            logger.warning(
                "llm structured output: model=%s rejected contract %s's JSON Schema "
                "(grammar compilation) — falling back to the PROMPTED path (bug 895c): %s",
                ran_model,
                getattr(model_cls, "__name__", model_cls),
                exc,
            )

    return _run_prompted_output(
        Agent, model, model_cls, req, kwargs, usage_limits, artifact_dir=artifact_dir
    )


def output_retry_allowance(req: RunRequest) -> int:
    """The output-retry allowance ``N`` for this request — the ONE value that seeds BOTH the
    Agent's ``retries={"output": N}`` and the ``UsageLimits.request_limit`` addend (RP-01 S2).

    ``structured.OUTPUT_RETRIES`` (2) is the default, lowered by ``req.structured_retry_limit``
    when the caller sets it (never raised above the default, never below zero). Seeding both
    budgets from this single value is what guarantees the shared request budget can NEVER trip
    before the shared output-retry counter."""
    from rebar.llm import structured

    allowance = structured.OUTPUT_RETRIES
    if req.structured_retry_limit is not None:
        allowance = min(allowance, max(0, int(req.structured_retry_limit)))
    return allowance


def apply_structured_seams(kwargs: dict, req: RunRequest, candidates, model_settings) -> None:
    """Merge the RP-01 S2 shared seams into an already-built Agent ``kwargs`` dict, in place.

    The wire-projection capability (seam 1) is APPENDED to any web capabilities already in
    ``kwargs`` so both ride one ``capabilities`` list, and the shared output-retry allowance N
    seeds ``retries={"output": N}`` — the counter that BOTH the concision-guard ``ModelRetry``
    and the ``TextOutput``-validator ``ModelRetry`` decrement. A ``text``-mode request runs no
    output retry, so it is left byte-identical (no wire projection, no ``retries``).

    Wiring only — the allowance is derived by :func:`output_retry_allowance` and the wire
    projection is built in :mod:`rebar.llm.pai_retry`; nothing is computed here. ``candidates``
    is the viable candidate-model set; the output reserve is the effective ``max_tokens``
    already computed into ``model_settings``."""
    if req.mode == "text":
        return
    from rebar.llm import pai_retry

    reserve = model_settings.get("max_tokens") if model_settings else None
    wire_cap = pai_retry.wire_history_processor(candidates, reserve)
    kwargs["capabilities"] = [*kwargs.get("capabilities", []), wire_cap]
    kwargs["retries"] = {"output": output_retry_allowance(req)}


def _agent_kwargs_with_guard(kwargs: dict, guard) -> dict:
    """A copy of ``kwargs`` with ``guard`` appended to its (already wire-seeded) capabilities —
    the per-branch guard (native terminal vs prompted concision-aware) is the only difference
    between the two output-mode Agents."""
    merged = dict(kwargs)
    merged["capabilities"] = [*merged.get("capabilities", []), guard]
    return merged


def _run_native_output(Agent, model, mode_obj, req: RunRequest, kwargs: dict, usage_limits):
    """The NativeOutput (constrained-decoding) branch as ONE bounded Agent run.

    Attaches ``pai_output.guard_capability()`` (terminal truncation — a fixed decoding grammar,
    not verbosity, drives a native turn's size, so a truncated native turn stays TERMINAL, no
    concision retry) alongside the shared wire projection and output-retry counter carried in
    ``kwargs``. Silent-success parity (story drake): ``check_response`` still runs so a
    truncated/refused turn degrades to INDETERMINATE rather than returning a hollow verdict."""
    from rebar.llm import pai_output, structured
    from rebar.llm.model_classes import ensure_current_event_loop

    agent = Agent(
        model,
        output_type=mode_obj,
        **_agent_kwargs_with_guard(kwargs, pai_output.guard_capability()),
    )
    ensure_current_event_loop()
    with usage_log.capture_attempt_messages():
        run_result = agent.run_sync(req.instructions, usage_limits=usage_limits)
    structured.check_response(run_result.response)
    return run_result.output, _extract_usage(run_result)


def _run_prompted_output(
    Agent,
    model,
    model_cls,
    req: RunRequest,
    kwargs: dict,
    usage_limits,
    *,
    artifact_dir: str | None = None,
):
    """The PROMPTED branch as ONE bounded Agent run (RP-01 S2).

    Composes the S1 output-policy adapter (``output_type=TextOutput(pai_output.output_function
    (model_cls))`` — the deterministic tolerant parse + validators) with the concision-aware
    guard (:func:`rebar.llm.pai_retry.concision_guard`), the shared wire projection, and the
    shared output-retry counter. The schema directive is appended so the model knows the EXACT
    output keys (free text otherwise conveys no schema). On output-retry exhaustion pydantic-ai
    raises ``UnexpectedModelBehavior``; it is translated to :class:`UnretryableOutputError`
    (still caught by every ``except StructuredOutputError`` / ``LLMRunnerError`` handler), and
    the LAST raw reply is captured to the opt-in parse-failure artifact (story 2fd6)."""
    from pydantic_ai import TextOutput, capture_run_messages
    from pydantic_ai.exceptions import UnexpectedModelBehavior

    from rebar.llm import pai_output, pai_retry, structured
    from rebar.llm.model_classes import ensure_current_event_loop

    agent = Agent(
        model,
        output_type=TextOutput(pai_output.output_function(model_cls)),
        **_agent_kwargs_with_guard(kwargs, pai_retry.concision_guard()),
    )
    prompt = f"{req.instructions}\n\n{structured.schema_directive(model_cls)}"
    ensure_current_event_loop()
    with capture_run_messages() as messages, usage_log.capture_attempt_messages():
        try:
            result = agent.run_sync(prompt, usage_limits=usage_limits)
        except UnexpectedModelBehavior as exc:
            if "Exceeded maximum output retries" not in str(exc):
                raise
            raise _exhausted_output_retries(
                exc, messages, model, req, artifact_dir, output_retry_allowance(req) + 1
            ) from exc
    return result.output, _extract_usage(result)


def _last_reply_text(messages) -> str:
    """The text of the LAST model response in ``messages`` (the exhausted run's final reply)."""
    from pydantic_ai.messages import ModelResponse, TextPart

    for msg in reversed(messages):
        if isinstance(msg, ModelResponse):
            texts = [p.content for p in getattr(msg, "parts", []) if isinstance(p, TextPart)]
            if texts:
                return "".join(texts)
    return ""


def _find_structured_output_error_on_chain(
    exc: BaseException | None,
) -> StructuredOutputError | None:
    """Walk ``exc``'s ``__cause__``/``__context__`` chain to arbitrary depth and return the
    nearest :class:`StructuredOutputError`, or ``None``. Cycle-safe (tracks visited ``id()``);
    prefers ``__cause__`` over ``__context__`` at each node."""
    seen: set[int] = set()
    while exc is not None and id(exc) not in seen:
        if isinstance(exc, StructuredOutputError):
            return exc
        seen.add(id(exc))
        exc = exc.__cause__ or exc.__context__
    return None


def _exhausted_output_retries(
    exc, messages, model, req: RunRequest, artifact_dir: str | None, attempts: int
) -> StructuredOutputError:
    """Translate a pydantic-ai output-retry exhaustion into a rebar-typed
    :class:`StructuredOutputError`, re-raising the original object preserved on the cause chain
    when present, and writing the opt-in raw-reply artifact when configured."""
    restored = _find_structured_output_error_on_chain(exc)
    path: str | None = None
    if artifact_dir:
        path = _write_parse_failure_artifact(
            artifact_dir,
            reply=_last_reply_text(messages),
            model=getattr(model, "model_name", None) or str(model),
            contract=req.output_schema or "",
            attempts=attempts,
        )
    if restored is not None:
        if path is not None:
            restored.args = (f"{restored} [raw reply captured: {path}]",)
        return restored
    if path is not None:
        return UnretryableOutputError(f"{exc} [raw reply captured: {path}]")
    return UnretryableOutputError(str(exc))


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
    # RP-01 S2: the CONSTRUCTED request_limit adds the output-retry allowance N so the shared
    # request budget can NEVER trip before the shared output-retry counter (both seeded from
    # the SAME `output_retry_allowance(req)`). The RETURNED `req_limit` stays the BARE base —
    # telemetry and `completion_banking.iteration_limit_for`'s `2*B` inverse read it, and the
    # allowance is an addend ONLY inside the constructed UsageLimits. A `text`-mode call runs no
    # output retry, so it gets the bare base (no addend) — byte-identical to the pre-S2 budget.
    allowance = 0 if req.mode == "text" else output_retry_allowance(req)
    usage_limits = UsageLimits(
        request_limit=req_limit + allowance,
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
