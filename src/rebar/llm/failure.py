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
from typing import Any

from rebar.llm.errors import LLMConfigError, UnretryableOutputError

# `httpx` + `pydantic_ai` are [agents]-extra deps, NOT core. They are imported LAZILY
# (call-boundary) inside `_map` / `_base_diagnostic` so this boundary module imports
# cleanly under a no-extras core install — the optionality contract, enforced by
# tests/unit/test_core_optionality.py. Classification only runs when a real LLM failure
# occurred (extra present); when the stack is ABSENT the classifier degrades to
# NEEDS_INVESTIGATION rather than raising ModuleNotFoundError (the totality guarantee).


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
    # The CONSUMED counters alongside their ceilings (df94): the gate durably recorded the
    # LIMIT (request_limit/tool_calls_limit) but silently dropped what was actually SPENT, so
    # a run's consumption was unobservable on the sidecar. Both are plain integers — a request
    # count and a tool-call count — carrying NO prompt/response/argument text, so they are safe
    # on the privacy contract this allowlist enforces (the sanitization boundary admits scalars
    # that name no content).
    "requests",
    "tool_calls",
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
# The SINGLE definition of the provider context-length phrasings (story fcb7).
# ``sizing.is_context_limit_error`` reads this tuple rather than carrying a copy: a
# context-length 400 classified CHANGE_INPUT here is the SAME error the escalation ladder
# there recognises, so two lists could drift apart and disagree. The dependency points
# only inward (plan_review -> here), keeping this low-level boundary module free of any
# dependency on the plan_review package.
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


def _http_error_body(exc: Any) -> dict:  # exc is a pydantic_ai ModelHTTPError (lazy-typed)
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
    try:
        from pydantic_ai.exceptions import ModelHTTPError, UsageLimitExceeded
    except ImportError:
        # [agents] stack absent — no pydantic_ai exception can be present; return the
        # base diagnostic unenriched (the exception is a rebar-typed / stdlib error).
        return diag
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


def _map_http(exc: Any) -> ResolutionClass:  # exc is a pydantic_ai ModelHTTPError (lazy-typed)
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
    try:
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
    except ImportError:
        # [agents] stack absent — none of the typed arms below can match. A no-extras
        # caller (e.g. runner.preflight classifying a missing-extra LLMConfigError) lands
        # here; classify as NEEDS_INVESTIGATION (non-retryable), never raise.
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

# A content-policy block (Bedrock Guardrail intervention or the model's own content
# filter / refusal) normalizes to ``finish_reason=content_filter`` and classifies
# CHANGE_INPUT — the correct disposition, but the generic CHANGE_INPUT message misreports
# the cause as "too large / malformed". This specialization names the real failure mode so
# the operator is not sent to the wrong (size/format) remedy. See :func:`message_for`.
CONTENT_FILTER_MESSAGE = (
    "The request was blocked by a content policy — a Bedrock Guardrail, or the model's own "
    "content filter / refusal (finish_reason=content_filter). This is NOT a size or "
    "formatting problem; adjust the input, or the guardrail configuration, and retry."
)


def message_for(resolution_class: str | None, *, finish_reason: str | None = None) -> str | None:
    """The human-facing stderr message for a persisted `coverage.resolution_class` value
    (a plain string), or ``None`` when the class is absent/unknown.

    ``finish_reason`` (when available on the outcome's / persisted ``diagnostic``)
    disambiguates the shared ``CHANGE_INPUT`` class: a ``content_filter`` block gets the
    guardrail/content-policy message, while genuine size/malformed CHANGE_INPUT cases
    (HTTP 413, context-length 400) keep the generic one. Omitting it preserves the prior
    behaviour for the string-only caller."""
    if not resolution_class:
        return None
    if finish_reason == "content_filter" and resolution_class == ResolutionClass.CHANGE_INPUT.value:
        return CONTENT_FILTER_MESSAGE
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


# ── Sampling-parameter rejection translation (story S3/2932) ────────────────────
# The `_MODEL_ID_CAPABILITY_OVERRIDES` denylist in capabilities.py will drift as AWS
# deprecates sampling params on more models than the ones rebar has MEASURED. An unlisted-but-
# affected model must fail LOUDLY and ACTIONABLY (name the model, the parameter, both
# remedies) rather than as an opaque provider outage classified LLMUnavailableError. This is
# rebar's existing failure-classification seam, not the Bedrock leaf — no provider-name branch
# enters `runner.py` for this; `runner.run()` tries this FIRST in its broad except, falling
# back to the existing classify_llm_failure/LLMUnavailableError path only when it returns None.
_SAMPLING_PARAMS: tuple[str, ...] = ("temperature", "top_p", "top_k")
_REJECTION_WORDS: tuple[str, ...] = ("deprecated", "not supported", "unsupported")


def translate_sampling_parameter_rejection(
    exc: BaseException, model_id: str
) -> LLMConfigError | None:
    """Return an :class:`LLMConfigError` if ``exc`` looks like a provider rejecting a sampling
    parameter (e.g. Bedrock's "temperature is deprecated for this model"), else ``None``.

    ALL THREE conjuncts are required, checked on ``exc`` or its ``__cause__`` (a provider SDK
    error is often wrapped):

    1. looks like a client rejection: ``status_code == 400`` OR ``"ValidationException"`` in
       ``str(exc)``;
    2. names a sampling parameter rebar can send: ``temperature``, ``top_p``, or ``top_k``;
    3. carries a rejection word: ``deprecated``, ``not supported``, or ``unsupported``.

    All three are required so an UNRELATED 400 (a malformed request, a bad model id) is not
    swallowed by this translation and keeps its existing ``LLMUnavailableError``
    classification — only a genuine sampling-parameter rejection is redirected here.
    """
    for candidate in (exc, getattr(exc, "__cause__", None)):
        if candidate is None:
            continue
        status_code = getattr(candidate, "status_code", None)
        text = str(candidate)
        looks_like_rejection = status_code == 400 or "ValidationException" in text
        if not looks_like_rejection:
            continue
        low = text.lower()
        named_param = next((p for p in _SAMPLING_PARAMS if p in low), None)
        if named_param is None:
            continue
        if not any(word in low for word in _REJECTION_WORDS):
            continue
        return LLMConfigError(
            f"model {model_id!r} rejected the {named_param!r} sampling parameter: {text}. "
            f"Fix by either (1) adding {model_id!r} to "
            "rebar.llm.capabilities._MODEL_ID_CAPABILITY_OVERRIDES with "
            f"{{'supports_{named_param}': False}} (if such a field exists — today only "
            "'supports_temperature' is tracked), or (2) unsetting the corresponding "
            f"config (e.g. REBAR_LLM_TEMPERATURE) so {named_param!r} is never sent."
        )
    return None


# ── Grammar/schema-complexity rejection translation (bug 895c) ──────────────────
# The exact sibling of `translate_sampling_parameter_rejection` above, for the OTHER way a
# healthy provider permanently refuses a well-formed rebar request: native/constrained
# decoding compiles the contract's JSON Schema into a grammar, and rebar's Pass-2
# verification contracts (22-36 OPTIONAL parameters) blow the documented compilation budget.
# Bedrock returns HTTP 400 `ValidationException` with "Grammar compilation timed out."
# (~185s) or "Schema is too complex." (~50-60s).
#
# This is emphatically NOT an `LLMUnavailableError`: that type means "provider outage, try
# later", and here the provider is HEALTHY and the identical request will fail identically
# forever — a retry ladder built on it burns ~185s per attempt and still fails. It is
# `UnretryableOutputError`, the existing type for "re-running the SAME call reliably
# reproduces this", which is also already a `StructuredOutputError`, so every existing
# `except StructuredOutputError` / `except LLMError` handler keeps catching it.
#
# TWO wire shapes, measured, with NO shared vocabulary — which is why the phrase set alone was
# never sufficient protection and the preflight gate in `structured.output_mode` is the
# load-bearing half; this translator is the safety net for whatever the gate under-predicts:
#
#   Variant A (newer botocore, e.g. 1.43.64) — the request reaches Bedrock and the grammar
#   fails to compile SERVER-side after ~185s: `status_code: 400 ValidationException` /
#   "The model returned the following errors: Grammar compilation timed out." (or "Schema is
#   too complex.").
#
#   Variant B (older botocore, e.g. 1.40.61) — boto3 rejects the request CLIENT-side in 0.0s,
#   before it is ever sent: `Parameter validation failed: Unknown parameter in input:
#   "outputConfig", must be one of: modelId, messages, system, inferenceConfig, …`. There is
#   no status code and no `ValidationException` here, so variant B needs its OWN conjunctive
#   rule — and it is a STACK fact, not a schema fact: it fired on a 722-byte contract.
_GRAMMAR_REJECTION_PHRASES: tuple[str, ...] = (
    "grammar compilation timed out",
    "schema is too complex",
    "compiled grammar",
)
# Variant B: botocore's own ParamValidationError wording. Required TOGETHER with an explicit
# `outputConfig` mention, so an unrelated botocore parameter-validation failure (a bad
# `inferenceConfig`, a malformed `toolConfig`) is not swept in here.
_PARAM_VALIDATION_PHRASES: tuple[str, ...] = (
    "unknown parameter",
    "parameter validation failed",
)


def translate_schema_complexity_rejection(
    exc: BaseException, model_id: str
) -> UnretryableOutputError | None:
    """Return an :class:`UnretryableOutputError` if ``exc`` says NATIVE STRUCTURED OUTPUT is
    unusable for this contract on this stack — else ``None``. (The name is kept as-is because
    it is the exported symbol; the scope is the schema/native-output seam, both variants.)

    Checked on ``exc`` or its ``__cause__`` (a provider SDK error is often wrapped). Two
    independent CONJUNCTIVE rules, one per measured wire shape — never a disjunction of loose
    keywords:

    * **Variant A — server-side grammar rejection.** BOTH of: (1) it looks like a client
      rejection — ``status_code == 400`` (pydantic-ai's ``ModelHTTPError`` carries it as an
      ATTRIBUTE) OR ``"ValidationException"`` in the text (raw boto3 has no status code); and
      (2) the text carries one of a small CLOSED set of documented grammar phrases
      (case-insensitive): ``grammar compilation timed out``, ``schema is too complex``,
      ``compiled grammar``.
    * **Variant B — client-side parameter rejection.** BOTH of: the text names
      ``outputConfig``, AND it carries botocore's parameter-validation wording
      (``unknown parameter`` / ``parameter validation failed``). This shape has NO status code
      and no ``ValidationException``, so it cannot ride variant A's first conjunct.

    Because each rule is conjunctive and phrase-closed, this cannot silently become a
    catch-all: an unrelated 400 (a deprecated ``temperature``, a malformed request), any 5xx,
    a connection error, and an existing ``LLMUnavailableError`` all keep their current
    classification and fall through to the generic ``LLMUnavailableError`` path.
    """
    for candidate in (exc, getattr(exc, "__cause__", None)):
        if candidate is None:
            continue
        text = str(candidate)
        low = text.lower()
        status_code = getattr(candidate, "status_code", None)
        looks_like_rejection = status_code == 400 or "ValidationException" in text
        if looks_like_rejection and any(p in low for p in _GRAMMAR_REJECTION_PHRASES):
            cause = "the provider could not compile it into a decoding grammar"
            remedy = (
                "(1) re-measuring rebar.llm.structured._NATIVE_MAX_OPTIONAL_PROPERTIES — that "
                "bound already routes over-complex contracts to the PROMPTED path, which "
                "returns the same verdict, so a contract reaching here has drifted past it; "
                "(2) shrinking the contract's optional-parameter count; or (3) clearing "
                f"native_structured_output for {model_id!r} in rebar.llm.capabilities"
            )
        elif "outputconfig" in low and any(p in low for p in _PARAM_VALIDATION_PHRASES):
            cause = (
                "the installed botocore rejected the native outputConfig parameter "
                "client-side, before the request was sent"
            )
            remedy = (
                "(1) upgrading botocore — 1.43.64 carries outputConfig on bedrock-runtime "
                "Converse, 1.40.61 does not; or (2) nothing, if the prompted fallback is "
                "acceptable: rebar.llm.structured.native_output_stack_supported already "
                "detects this stack and routes every contract to the PROMPTED path"
            )
        else:
            continue
        return UnretryableOutputError(
            f"model {model_id!r} could not be called with native structured output — {cause}: "
            f"{text}. This is NOT a provider outage: the identical request will fail "
            f"identically on every retry. Fix by either {remedy}."
        )
    return None


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


def recovery_failure_cause(exc: object) -> str | None:
    """Why a bounded-completion-recovery failure happened, or ``None`` if ``exc`` isn't one.

    Returns a sentence fragment for a caller to embed, the same shape :func:`message_for`
    returns — the caller owns its own command phrasing; this owns the explanation.

    It belongs beside :func:`message_for` and :func:`outcome_of` because it answers their
    question ("what should the operator be told about this failure?") for the one failure
    class they structurally cannot: ``CompletionRecoveryError`` is raised inside the
    recovery module and never crosses the runner seams that attach an ``.outcome``, so
    :func:`outcome_of` returns ``None`` and :func:`message_for` has nothing to look up.
    Without this, such a failure silently inherits whatever default the caller uses for an
    unavailable runtime.

    The size claim is deliberately CONDITIONAL: recovery raises this error at several
    stages and only the bound checks carry ``context_chars``/``context_char_limit``.
    Reading them unconditionally yields "context (None chars) exceeds the limit (None
    chars)" for, say, an exhaustion at criterion 7 — relocating the misdiagnosis instead
    of removing it.

    The size branch deliberately does NOT tell the operator to "shorten the ticket's
    description/comments" — that remedy reads as actionable but neither half is:

    * **description** — the description is *material*: :func:`rebar.llm.plan_review.pass1
      .material_fingerprint` hashes it into the plan-review attestation, and its docstring
      records that a material edit invalidates the signature. Shortening it to clear this
      close gate would therefore trip the other one (``require_plan_review_for_close``),
      which then demands the attestation be re-earned. The wording keeps the lever but
      states that trade, rather than sending the operator into it blind.
    * **comments** — comments are explicitly NON-material (same docstring), so editing
      them would be attestation-safe, but the event store is append-only: ``comment()``
      in ``rebar._lib_writes`` (see the public write surface around line 511) has no
      delete or redact counterpart. There is no operation that shortens or removes a
      comment, so advising it names a lever that does not exist.

    What is left is the measurement (which is the real diagnosis) plus the pointer to the
    ``gate_error_v1`` sidecar — a step the operator can actually take.
    """
    from rebar.llm.errors import CompletionRecoveryError

    if not isinstance(exc, CompletionRecoveryError):
        return None
    diag = getattr(exc, "diagnostic", None) or {}
    chars, limit = diag.get("context_chars"), diag.get("context_char_limit")
    if isinstance(chars, int) and isinstance(limit, int):
        return (
            f"The ticket's context ({chars:,} chars) exceeds the bounded completion-recovery "
            f"limit ({limit:,} chars), so the recovery pass was refused before it ran. The "
            f"context cannot simply be made smaller: its comments are append-only (the store "
            f"has no redaction operation), and its description is material, so editing it "
            f"invalidates the plan-review attestation, which would then have to be re-earned. "
            f"Its gate_error_v1 sidecar holds the full diagnostic."
        )
    return (
        "Bounded completion recovery could not finish for this ticket; the recovery bounds "
        "are deliberate safeguards, not a transient fault. Its gate_error_v1 sidecar holds "
        "the full diagnostic."
    )


UNAVAILABLE_REMEDY = (
    "The completion-verification gate is enabled "
    "(verify.require_completion_verification_for_close); install the 'agents' extra "
    "and set a model API key."
)


def _is_unavailable_runtime(exc: object, outcome: LLMOutcome | None) -> bool:
    """True only when the verifier could not RUN — a missing extra/credential/setting."""

    if outcome is not None and outcome.resolution_class is ResolutionClass.CHANGE_SETTINGS:
        return True
    if isinstance(exc, ImportError):  # the [agents] extra is not installed at all
        return True
    from rebar.llm.errors import LLMUnavailableError

    return isinstance(exc, LLMUnavailableError)


def close_gate_remedy(exc: object, outcome: LLMOutcome | None = None) -> str:
    """The remedy sentence the close gate prints for a completion-verifier failure.

    Three dispositions, because they need three different operator actions:

    * a bounded-recovery breach -> :func:`recovery_failure_cause` (size/stage + sidecar);
    * a genuine UNAVAILABILITY (no ``[agents]`` extra, no API key, or any classified
      ``CHANGE_SETTINGS`` outcome) -> :data:`UNAVAILABLE_REMEDY`, which is correct there;
    * anything else -> the failure's OWN remedy. A verifier that spent minutes making real
      model calls and then exhausted its step budget is not an uninstalled extra, and
      telling the operator to install one sends them to a dead end while burying the
      actionable ``REBAR_LLM_MAX_STEPS`` guidance the exception already carries.
    """

    cause = recovery_failure_cause(exc)
    if cause:
        return cause
    if _is_unavailable_runtime(exc, outcome):
        return UNAVAILABLE_REMEDY
    detail = str(exc).strip()
    if detail:
        return (
            "The completion verifier ran and then failed, so this is not a missing install "
            f"or credential — act on its own reported remedy: {detail}"
        )
    return (
        "The completion verifier ran and then failed without a remedy of its own; inspect "
        "the session log's sanitized diagnostic."
    )
