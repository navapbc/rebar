"""Report ineffective LLM prompt caching and estimate the marked prefix.

These diagnostics are warning-only observability and must never raise or block. ``runner`` and
``usage_report`` call into this leaf, which has no runtime import back to the runner or a heavy
provider library at module scope.
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
    """Warn when requested prompt caching has no useful effect.

    The predicate requires nonzero billed input and remains separate from zero-usage detection.
    A caller supplies the model-specific cache floor. With a known marked-prefix size, an
    above-floor zero-read and zero-write call reports a cache that did not engage. A below-floor
    prefix reports misplaced payload only when at least one floor of input sits outside the
    breakpoint. Unknown prefix size preserves the total-input fallback. All outcomes remain
    warning-only because ineffective caching affects cost rather than correctness."""
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
    """Return whether every caching call wrote data and none read it.

    At least ``min_calls`` calls must report a cache write or read. A single initial write is
    expected, while repeated write-only calls reveal a run-level problem that per-call diagnostics
    cannot identify. Runs with no caching calls remain outside this predicate."""
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
    """Estimate tokens before the cache breakpoint, or return ``None`` when unknown.

    The instructions breakpoint places the system prompt in the marked prefix. A later
    multi-turn message breakpoint can only enlarge that cached span. Tool definitions are omitted
    because their serialized form is unavailable here, which conservatively undercounts and can
    only suppress a warning. No instructions breakpoint returns ``None`` and selects the existing
    total-input fallback. The shared characters-per-token estimate is imported lazily to preserve
    this leaf's dependency direction."""
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
