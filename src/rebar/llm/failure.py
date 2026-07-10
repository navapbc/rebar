"""LLM failure classification — the closed resolution-disposition vocabulary, a PURE,
total classifier, and a diagnostic sanitizer (story civilized-immediate-mamba, epic
jira-reb-687).

A stable boundary module: the runner attaches an :class:`LLMOutcome` at its generic
failure seam, and story polite-dutiful-drake imports :func:`classify_llm_failure` /
:func:`sanitize_diagnostic` from here to route silent-success failures through the SAME
construction. This module adds classification METADATA only — it changes NO exception
type raised anywhere in the runner (the ``UsageLimitExceeded`` arm, the structured
``UnretryableOutputError`` fast-fail, and ``sizing.is_context_limit_error`` are all
untouched); the per-seam wiring + exit codes belong to story authorial-hated-blackbear.

Failures surface at SEVERAL seams (empirically pinned by tests/unit/test_llm_failure_matrix.py):
a dedicated ``UsageLimitExceeded`` arm, the structured ``finish_reason`` path, rebar's own
typed-error passthrough, and the generic ``except Exception``. The classifier therefore
accepts an exception from ANY seam OR a ``finish_reason`` carried on the context, and is
TOTAL: an unmatched failure maps to ``NEEDS_INVESTIGATION`` and the classifier never raises.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

import httpx
from pydantic_ai.exceptions import (
    ContentFilterError,
    FallbackExceptionGroup,
    IncompleteToolCall,
    ModelAPIError,
    ModelHTTPError,
    UsageLimitExceeded,
    UserError,
)


class ResolutionClass(str, Enum):
    """The closed 8-class resolution-disposition vocabulary. ``retryable`` (below) is the
    behaviour-critical bit; the class itself is the messaging/telemetry hint."""

    WAIT_AND_RETRY = "WAIT_AND_RETRY"
    RETRY_NOW = "RETRY_NOW"
    INCREASE_PROVIDER_LIMITS = "INCREASE_PROVIDER_LIMITS"
    CHANGE_SETTINGS = "CHANGE_SETTINGS"
    CHANGE_INPUT = "CHANGE_INPUT"
    CHANGE_PROVIDER_OR_MODEL = "CHANGE_PROVIDER_OR_MODEL"
    FIX_AGENT_DESIGN = "FIX_AGENT_DESIGN"
    NEEDS_INVESTIGATION = "NEEDS_INVESTIGATION"


_RETRYABLE: frozenset[ResolutionClass] = frozenset(
    {ResolutionClass.WAIT_AND_RETRY, ResolutionClass.RETRY_NOW}
)


@dataclass(frozen=True)
class ClassifyContext:
    """Optional diagnostic-enrichment inputs folded into the diagnostic. Fully optional —
    ``classify_llm_failure(exc)`` works with none. ``finish_reason`` lets the structured
    seam classify a truncation/content-filter signal that raised no exception."""

    model: str | None = None
    provider: str | None = None
    attempts: int | None = None
    trace_id: str | None = None
    finish_reason: str | None = None


@dataclass(frozen=True)
class LLMOutcome:
    """A classified failure: the disposition, a SANITIZED diagnostic, and whether a retry
    could plausibly help."""

    resolution_class: ResolutionClass
    diagnostic: dict
    retryable: bool


# ── Diagnostic sanitization: allowlist + redactor ─────────────────────────────
# The diagnostic is built from an ALLOWLIST — the raw provider body is never dumped
# wholesale. Any free-text captured passes through the redactor below.
_ALLOWED_FIELDS: tuple[str, ...] = (
    "exception_type",
    "status_code",
    "error_code",
    "error_type",
    "finish_reason",
    "retry_after",
    "attempts",
    "trace_id",
    "model",
    "provider",
    "request_limit",
    "tool_calls_limit",
    "message",
)

# Ordered redactions (key-shaped tokens BEFORE generic hex/base64 so a labelled key is not
# half-masked). Each is a (compiled pattern, replacement).
_REDACTIONS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"sk-ant-[A-Za-z0-9_\-]{6,}"), "[REDACTED_KEY]"),
    (re.compile(r"sk-[A-Za-z0-9_\-]{12,}"), "[REDACTED_KEY]"),
    (re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._\-]{8,}"), "Bearer [REDACTED]"),
    (
        re.compile(
            r"(?i)(authorization|x-api-key|api[_-]?key|cookie|set-cookie)"
            r"(\"?\s*[:=]\s*\"?)([^\s\"',}]+)"
        ),
        r"\1\2[REDACTED]",
    ),
    (re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}"), "[REDACTED_EMAIL]"),
    (re.compile(r"\b[A-Fa-f0-9]{32,}\b"), "[REDACTED_HEX]"),
    (re.compile(r"\b[A-Za-z0-9+/]{40,}={0,2}\b"), "[REDACTED_B64]"),
)


def _redact(text: str) -> str:
    for pattern, repl in _REDACTIONS:
        text = pattern.sub(repl, text)
    return text


def sanitize_diagnostic(fields: dict) -> dict:
    """Return an allowlisted, redacted copy of ``fields`` — string values pass through the
    secret redactor; unknown keys are dropped; the raw body is never included wholesale."""
    out: dict = {}
    for key in _ALLOWED_FIELDS:
        val = fields.get(key)
        if val is None:
            continue
        out[key] = _redact(val) if isinstance(val, str) else val
    return out


# ── Classification ────────────────────────────────────────────────────────────
# Mirrors the phrasing list in ``sizing.is_context_limit_error`` (kept local so this
# low-level boundary module has no dependency on the plan_review package — the two must
# stay in sync; a context-length 400 classified CHANGE_INPUT here is the SAME error the
# ladder there recognises, and this story changes no raised type so the ladder is intact).
_CONTEXT_LEN_HINTS: tuple[str, ...] = (
    "context",
    "too many tokens",
    "maximum context",
    "context_length",
    "prompt is too long",
    "input length",
    "exceeds the maximum",
    "token limit",
)


def _unwrap_retry_error(exc: BaseException | None) -> BaseException | None:
    """Unwrap a ``tenacity.RetryError`` to the underlying last-attempt exception so an
    exhausted-retry ``httpx.ReadTimeout`` classifies as WAIT_AND_RETRY, not
    NEEDS_INVESTIGATION. GUARDED: tenacity is absent until story arcticduck adds it (wave
    1 has no tenacity), so the import failing is a safe no-op."""
    try:
        import tenacity
    except ImportError:
        return exc
    if isinstance(exc, tenacity.RetryError) and exc.last_attempt is not None:
        try:
            inner = exc.last_attempt.exception()
        except Exception:  # noqa: BLE001 — best-effort unwrap; fall back to the wrapper
            return exc
        if inner is not None:
            return inner
    return exc


def _http_error_body(exc: ModelHTTPError) -> dict:
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            return err
    return {}


def _base_diagnostic(exc: BaseException | None, ctx: ClassifyContext) -> dict:
    diag: dict = {
        "exception_type": type(exc).__name__ if exc is not None else None,
        "message": str(exc) if exc is not None else None,
        "model": ctx.model,
        "provider": ctx.provider,
        "attempts": ctx.attempts,
        "trace_id": ctx.trace_id,
        "finish_reason": ctx.finish_reason,
    }
    if isinstance(exc, ModelHTTPError):
        diag["status_code"] = exc.status_code
        err = _http_error_body(exc)
        diag["error_type"] = err.get("type")
        diag["error_code"] = err.get("code")
    if isinstance(exc, UsageLimitExceeded):
        # Disambiguate a genuine step budget (request_limit) from a stuck/looping tool
        # (tool_calls_limit) — the message names which limit tripped.
        msg = str(exc).lower()
        diag["tool_calls_limit"] = "tool_calls" in msg or "tool call" in msg
        diag["request_limit"] = "request_limit" in msg or "request limit" in msg
    return diag


def _map_http(exc: ModelHTTPError) -> ResolutionClass:
    status = exc.status_code
    err = _http_error_body(exc)
    code = (err.get("type") or err.get("code") or "").lower()
    msg = str(exc).lower()
    if status == 429:
        if "insufficient_quota" in code or "insufficient_quota" in msg:
            return ResolutionClass.INCREASE_PROVIDER_LIMITS
        return ResolutionClass.WAIT_AND_RETRY  # rate_limit / overloaded
    if status == 529 or status in (500, 502, 503, 504):
        return ResolutionClass.WAIT_AND_RETRY
    if status == 402:
        return ResolutionClass.INCREASE_PROVIDER_LIMITS
    if status in (401, 403):
        return ResolutionClass.CHANGE_SETTINGS
    if status == 413:
        return ResolutionClass.CHANGE_INPUT
    if status == 400:
        if any(h in msg for h in _CONTEXT_LEN_HINTS):
            return ResolutionClass.CHANGE_INPUT
        return ResolutionClass.CHANGE_SETTINGS  # malformed / invalid request
    return ResolutionClass.NEEDS_INVESTIGATION


def _map(exc: BaseException | None, ctx: ClassifyContext) -> ResolutionClass:
    # A content-filter / truncation finish_reason carried from the structured seam — the
    # primary use when NO exception was raised (exc is None).
    if ctx.finish_reason in ("content_filter",):
        return ResolutionClass.CHANGE_INPUT
    if exc is None:
        return ResolutionClass.NEEDS_INVESTIGATION
    # httpx transport errors (caught directly; ConnectTimeout is NOT a ConnectError).
    # ORDER MATTERS: ModelHTTPError IS-A ModelAPIError, so it is matched first (below).
    if isinstance(exc, (httpx.ConnectError, httpx.ConnectTimeout)):
        return ResolutionClass.RETRY_NOW
    if isinstance(exc, httpx.TimeoutException):  # ReadTimeout / PoolTimeout / WriteTimeout
        return ResolutionClass.WAIT_AND_RETRY
    if isinstance(exc, ModelHTTPError):
        return _map_http(exc)
    if isinstance(exc, ContentFilterError):
        return ResolutionClass.CHANGE_INPUT
    if isinstance(exc, IncompleteToolCall):
        return ResolutionClass.FIX_AGENT_DESIGN
    if isinstance(exc, UsageLimitExceeded):
        return ResolutionClass.FIX_AGENT_DESIGN
    if isinstance(exc, UserError):
        return ResolutionClass.CHANGE_SETTINGS
    if isinstance(exc, FallbackExceptionGroup):
        return ResolutionClass.CHANGE_PROVIDER_OR_MODEL
    if isinstance(exc, ModelAPIError):  # no status_code (ModelHTTPError handled above)
        return ResolutionClass.RETRY_NOW
    return ResolutionClass.NEEDS_INVESTIGATION


def classify_llm_failure(
    exc: BaseException | None = None, ctx: ClassifyContext | None = None
) -> LLMOutcome:
    """Classify a failure into an :class:`LLMOutcome`. ``exc`` is the raised exception from
    ANY runner seam; it may be ``None`` when the only signal is a ``finish_reason`` carried
    on ``ctx`` (the structured content-filter/truncation case, which raises nothing). TOTAL:
    never raises — an unmatched failure, or an internal error (including a sanitizer
    failure), degrades to ``NEEDS_INVESTIGATION`` with a minimal diagnostic."""
    ctx = ctx or ClassifyContext()
    try:
        unwrapped = _unwrap_retry_error(exc)
        resolution = _map(unwrapped, ctx)
        diagnostic = sanitize_diagnostic(_base_diagnostic(unwrapped, ctx))
        return LLMOutcome(resolution, diagnostic, retryable=resolution in _RETRYABLE)
    except Exception:  # noqa: BLE001 — totality guard: classification must never raise
        try:
            placeholder = {
                "exception_type": type(exc).__name__,
                "message": "diagnostic unavailable",
            }
        except Exception:  # noqa: BLE001 — even type() is defended
            placeholder = {"message": "diagnostic unavailable"}
        return LLMOutcome(ResolutionClass.NEEDS_INVESTIGATION, placeholder, retryable=False)


# ── Disposition surfacing (story authorial-hated-blackbear, epic jira-reb-687) ──
# The human-facing message per class, printed to stderr by the gate CLIs. The retryable
# pair (WAIT_AND_RETRY/RETRY_NOW) drive exit 11; the rest map to the existing INDETERMINATE
# exit. Keyed by the class VALUE (== name) so it is readable straight off a persisted
# `coverage.resolution_class` string, with no enum round-trip.
RESOLUTION_MESSAGES: dict[str, str] = {
    ResolutionClass.WAIT_AND_RETRY.value: (
        "Transient provider overload/rate-limit — wait for the backoff window, then retry."
    ),
    ResolutionClass.RETRY_NOW.value: "Transient connection blip — retry immediately.",
    ResolutionClass.INCREASE_PROVIDER_LIMITS.value: (
        "Provider usage/quota ceiling hit — raise the provider limit or wait for the reset."
    ),
    ResolutionClass.CHANGE_SETTINGS.value: (
        "The request exceeded a configured bound (tokens/steps/timeout) — adjust it and retry."
    ),
    ResolutionClass.CHANGE_INPUT.value: (
        "The input was rejected (too large / malformed) — reduce or fix it and retry."
    ),
    ResolutionClass.CHANGE_PROVIDER_OR_MODEL.value: (
        "The model/provider is unavailable or refused — switch model/provider."
    ),
    ResolutionClass.FIX_AGENT_DESIGN.value: (
        "An agent-construction bug (no tools / bad output contract) — fix the op wiring."
    ),
    ResolutionClass.NEEDS_INVESTIGATION.value: (
        "Unclassified failure — inspect the sanitized diagnostic."
    ),
}


def message_for(resolution_class: str | None) -> str | None:
    """The human-facing stderr message for a persisted `coverage.resolution_class` value
    (a plain string), or ``None`` when the class is absent/unknown."""
    if not resolution_class:
        return None
    return RESOLUTION_MESSAGES.get(resolution_class)


def resolution_fields(outcome: LLMOutcome | None) -> dict:
    """The `coverage` fields a degraded verdict carries for an ``LLMOutcome`` — the persisted
    disposition the CLIs read: ``resolution_class`` (str), ``retryable`` (bool), ``diagnostic``
    (sanitized dict). Returns ``{}`` when there is no outcome (e.g. a string-error degrade path
    that never classified), so the caller writes nothing and the verdict stays a plain
    INDETERMINATE. This is the single writer of the disposition shape onto ``coverage``."""
    if outcome is None:
        return {}
    return {
        "resolution_class": outcome.resolution_class.value,
        "retryable": outcome.retryable,
        "diagnostic": outcome.diagnostic,
    }


def outcome_of(error: object) -> LLMOutcome | None:
    """The `.outcome` an ``LLMOutcome`` producer attached to a raised error (mamba's run seam,
    the preflight seam), or ``None`` when the error carries none (a string, or an unclassified
    raise). Never raises."""
    return getattr(error, "outcome", None)


def log_degrade(outcome: LLMOutcome | None, *, gate: str, ticket_id: str | None = None) -> None:
    """Best-effort: append the SANITIZED diagnostic of a degraded gate run to the session log,
    so an operator can later surface the failure by keyword. A no-op when there is no outcome.
    NEVER raises — a session-log write failure must not fail the gate (the whole point of the
    degrade path is to fail *softly*); the import is lazy so importing this module stays light."""
    if outcome is None:
        return
    try:
        import json as _json

        import rebar as _rebar

        entry = _json.dumps(outcome.diagnostic, sort_keys=True, default=str)
        _rebar.append_session_log(
            entry,
            summary=f"llm-degrade {outcome.resolution_class.value} on {gate}",
            relates_to=ticket_id,
        )
    except Exception:  # noqa: BLE001 — telemetry is strictly best-effort; never fail the gate
        pass
