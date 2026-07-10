"""The LLM-failure classifier: the closed resolution-disposition mapping, the diagnostic
sanitizer, totality, and the metadata-only runner touch (story civilized-immediate-mamba,
epic jira-reb-687). Offline, no billable call.

The classifier is a PURE function: every row of the mapping is pinned by calling it
DIRECTLY with a concrete signal, independent of where it is wired in the runner. This story
raises NO new exception type; it only attaches an ``LLMOutcome`` at the runner's generic seam.
"""

from __future__ import annotations

import httpx
import pytest

pytest.importorskip("pydantic_ai")

from pydantic_ai.exceptions import (
    ContentFilterError,
    FallbackExceptionGroup,
    IncompleteToolCall,
    ModelAPIError,
    ModelHTTPError,
    UsageLimitExceeded,
    UserError,
)

from rebar.llm.failure import (
    ClassifyContext,
    LLMOutcome,
    ResolutionClass,
    classify_llm_failure,
    sanitize_diagnostic,
)

pytestmark = pytest.mark.unit
RC = ResolutionClass


def _http(status: int, **err) -> ModelHTTPError:
    return ModelHTTPError(status, "m", body={"error": err} if err else {})


# ── The complete mapping vocabulary — pinned by DIRECT calls ───────────────────
CASES = [
    ("429-rate", _http(429, type="rate_limit_error"), RC.WAIT_AND_RETRY, True),
    ("429-overloaded", _http(429, type="overloaded_error"), RC.WAIT_AND_RETRY, True),
    ("529", _http(529, type="overloaded_error"), RC.WAIT_AND_RETRY, True),
    ("500", _http(500), RC.WAIT_AND_RETRY, True),
    ("503", _http(503), RC.WAIT_AND_RETRY, True),
    ("read-timeout", httpx.ReadTimeout("read timed out"), RC.WAIT_AND_RETRY, True),
    ("pool-timeout", httpx.PoolTimeout("pool"), RC.WAIT_AND_RETRY, True),
    ("connect-timeout", httpx.ConnectTimeout("connect"), RC.RETRY_NOW, True),
    ("connect-error", httpx.ConnectError("conn"), RC.RETRY_NOW, True),
    ("model-api-no-status", ModelAPIError("m", "transient"), RC.RETRY_NOW, True),
    ("429-quota", _http(429, type="insufficient_quota"), RC.INCREASE_PROVIDER_LIMITS, False),
    ("402", _http(402), RC.INCREASE_PROVIDER_LIMITS, False),
    ("401", _http(401, type="authentication_error"), RC.CHANGE_SETTINGS, False),
    ("403", _http(403, type="permission_error"), RC.CHANGE_SETTINGS, False),
    ("400-malformed", _http(400, type="invalid_request_error"), RC.CHANGE_SETTINGS, False),
    ("usererror", UserError("bad model string"), RC.CHANGE_SETTINGS, False),
    (
        "400-context-length",
        ModelHTTPError(400, "m", body={"error": {"message": "prompt is too long"}}),
        RC.CHANGE_INPUT,
        False,
    ),
    ("413", _http(413, type="request_too_large"), RC.CHANGE_INPUT, False),
    ("content-filter-exc", ContentFilterError("refused"), RC.CHANGE_INPUT, False),
    (
        "fallback-group",
        FallbackExceptionGroup("exhausted", [ValueError("a")]),
        RC.CHANGE_PROVIDER_OR_MODEL,
        False,
    ),
    (
        "usage-limit",
        UsageLimitExceeded("exceeded the request_limit of 3"),
        RC.FIX_AGENT_DESIGN,
        False,
    ),
    ("incomplete-tool", IncompleteToolCall("incomplete"), RC.FIX_AGENT_DESIGN, False),
    ("unknown", RuntimeError("some novel failure"), RC.NEEDS_INVESTIGATION, False),
]


@pytest.mark.parametrize(
    ("exc", "exp_class", "exp_retry"),
    [(e, c, r) for _id, e, c, r in CASES],
    ids=[i for i, *_ in CASES],
)
def test_classify_maps_each_concrete_signal(exc, exp_class, exp_retry):
    out = classify_llm_failure(exc)
    assert isinstance(out, LLMOutcome)
    assert out.resolution_class is exp_class
    assert out.retryable is exp_retry


def test_content_filter_finish_reason_via_ctx():
    """A content-filter finish_reason (no exception raised) classifies via ctx."""
    out = classify_llm_failure(RuntimeError("x"), ClassifyContext(finish_reason="content_filter"))
    assert out.resolution_class is RC.CHANGE_INPUT


def test_connect_timeout_is_not_a_connect_error():
    """httpx.ConnectTimeout is a TimeoutException, NOT a ConnectError — but both map to
    RETRY_NOW; ReadTimeout (also a TimeoutException) maps to WAIT_AND_RETRY. Pin the split."""
    assert not issubclass(httpx.ConnectTimeout, httpx.ConnectError)
    assert classify_llm_failure(httpx.ConnectTimeout("c")).resolution_class is RC.RETRY_NOW
    assert classify_llm_failure(httpx.ReadTimeout("r")).resolution_class is RC.WAIT_AND_RETRY


def test_tenacity_retry_error_is_unwrapped():
    """An exhausted-retry RetryError wrapping an httpx.ReadTimeout classifies as
    WAIT_AND_RETRY (the named regression), not NEEDS_INVESTIGATION."""
    tenacity = pytest.importorskip("tenacity")
    try:
        for attempt in tenacity.Retrying(stop=tenacity.stop_after_attempt(1), reraise=False):
            with attempt:
                raise httpx.ReadTimeout("read timed out")
    except tenacity.RetryError as re:
        assert classify_llm_failure(re).resolution_class is RC.WAIT_AND_RETRY


def test_usage_limit_diagnostic_disambiguates_limit():
    out = classify_llm_failure(UsageLimitExceeded("the tool_calls_limit of 8 was exceeded"))
    assert out.resolution_class is RC.FIX_AGENT_DESIGN
    assert out.diagnostic.get("tool_calls_limit") is True


# ── Sanitization ──────────────────────────────────────────────────────────────
def test_sanitize_scrubs_planted_secrets_and_drops_unknown_fields():
    d = sanitize_diagnostic(
        {
            "message": (
                "Authorization: Bearer sk-ant-api03-DEADBEEFdeadbeef1234567890 for user@example.com"
            ),
            "status_code": 401,
            "raw_body_dump": "should be dropped",
        }
    )
    assert "sk-ant-api03-DEADBEEF" not in d["message"]
    assert "user@example.com" not in d["message"]
    assert "[REDACTED" in d["message"]
    assert d["status_code"] == 401
    assert "raw_body_dump" not in d  # not on the allowlist


def test_classifier_survives_a_sanitizer_failure(monkeypatch):
    """If the redactor blows up, the classifier degrades to NEEDS_INVESTIGATION rather
    than propagating (totality)."""
    import rebar.llm.failure as failure

    def _boom(_text):
        raise ValueError("redactor exploded")

    monkeypatch.setattr(failure, "_redact", _boom)
    out = classify_llm_failure(ModelHTTPError(429, "m", body={}))
    assert out.resolution_class is RC.NEEDS_INVESTIGATION
    assert out.retryable is False


# ── Totality ──────────────────────────────────────────────────────────────────
class _StrExplodes(Exception):
    def __str__(self) -> str:  # noqa: D105
        raise ValueError("str() itself raises")


@pytest.mark.parametrize(
    "exc",
    [
        RuntimeError("x"),
        ValueError("y"),
        KeyError("k"),
        BaseException("base"),
        _StrExplodes(),
        Exception(),
    ],
)
def test_classifier_is_total_and_never_raises(exc):
    out = classify_llm_failure(exc)
    assert isinstance(out, LLMOutcome)
    assert isinstance(out.resolution_class, ResolutionClass)


# ── Preservation: this story raises NO new type at the existing seams ──────────
def test_context_length_error_still_detected_by_the_ladder_predicate():
    """The classifier maps a context-length 400 to CHANGE_INPUT, but changes NO raised
    type — sizing.is_context_limit_error still recognises it, so the escalation ladder is
    intact."""
    from rebar.llm.errors import LLMUnavailableError
    from rebar.llm.plan_review.sizing import is_context_limit_error

    # The runner wraps the provider error, preserving the message the ladder keys on.
    wrapped = LLMUnavailableError("the LLM provider call failed: prompt is too long: ...")
    assert is_context_limit_error(wrapped) is True


def test_runner_attaches_llm_outcome_at_the_generic_seam():
    """The metadata-only runner touch: a systemic provider failure (injected 429) raises
    LLMUnavailableError as before, now carrying an ``outcome`` LLMOutcome (WAIT_AND_RETRY)
    — the raised TYPE is unchanged, so every existing catch still works."""
    import pydantic_ai.models
    from pydantic_ai.messages import ModelResponse, TextPart  # noqa: F401
    from pydantic_ai.models.function import FunctionModel

    from rebar.llm.config import LLMConfig
    from rebar.llm.errors import LLMUnavailableError
    from rebar.llm.runner import PydanticAIRunner, RunRequest

    pydantic_ai.models.ALLOW_MODEL_REQUESTS = False

    def _raise_429(messages, info):
        raise ModelHTTPError(429, "m", body={"error": {"type": "rate_limit_error"}})

    cfg = LLMConfig(repo_path=".")
    req = RunRequest(
        system_prompt="s", instructions="i", config=cfg, reviewers=["v"], mode="findings"
    )
    with pytest.raises(LLMUnavailableError) as ei:
        PydanticAIRunner(cfg, model_override=FunctionModel(_raise_429)).run(req)
    outcome = getattr(ei.value, "outcome", None)
    assert isinstance(outcome, LLMOutcome)
    assert outcome.resolution_class is RC.WAIT_AND_RETRY
    assert outcome.retryable is True


def test_check_stop_reason_still_fast_fails_on_truncation():
    """content_filter/length still raise UnretryableOutputError at the structured seam —
    this story adds classification metadata, it does not move that behavior."""
    from rebar.llm import structured
    from rebar.llm.errors import UnretryableOutputError

    for reason in ("content_filter", "length", "max_tokens", "refusal"):
        with pytest.raises(UnretryableOutputError):
            structured.check_stop_reason(reason)
    assert structured.check_stop_reason("stop") is None  # a normal finish is fine
