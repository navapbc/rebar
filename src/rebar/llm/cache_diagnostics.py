"""Cache-effectiveness DIAGNOSTICS for LLM runs — telemetry-only warnings that a requested
prompt cache silently did nothing, plus the marked-prefix estimator they need.

Split out of ``structured_run.py`` (task solitary-burly-acouchi) along the existing
call-graph seam: these functions are pure/observability code that the run mechanism only
CALLS, never depends on structurally. The dependency direction is one-way — ``runner`` and
``usage_report`` call in, nothing here imports ``structured_run`` back — so future
cache-warning logic lands here instead of re-inflating the orchestrator toward the 800-LOC
cap.

Everything here is WARNING-level observability and MUST NEVER raise or block: an ineffective
cache is a COST problem, not a correctness one.

Leaf module, same convention as ``structured_run``: no runtime import of ``runner``, and no
heavy provider library at module top, so ``import rebar.llm`` stays stdlib-only.
"""

from __future__ import annotations

import logging
from typing import Any

from rebar.llm.capabilities import CACHE_MIN_PREFIX_TOKENS

logger = logging.getLogger(__name__)


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
