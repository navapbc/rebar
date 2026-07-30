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
from typing import TYPE_CHECKING

from rebar.llm import usage_log
from rebar.llm.errors import StructuredOutputError, UnretryableOutputError

if TYPE_CHECKING:
    from rebar.llm.runner import RunRequest

logger = logging.getLogger(__name__)

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


def effective_max_iterations(floor: int, requested: int | None) -> int:
    """The PER-REQUEST agent step budget (bug 59bc). A caller may RAISE the budget for a single
    call by carrying a higher ``max_iterations`` on its ``RunRequest.config`` (e.g. the Pass-2
    verifier scaled by its finding count), without mutating a shared runner's ``self._config``
    under other steps. The request can only raise the operator-configured floor, never lower it —
    so ``max(floor, requested)``; a missing/None request value leaves the floor untouched."""
    return max(floor, requested or floor)


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
