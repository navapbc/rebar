"""Bug 1c0d — a retryable primary throttle must NOT be masked as INDETERMINATE by a
fallback leg's non-retryable failure.

The incident: under a shared Bedrock account the whole fleet throttles at once. The
frontier chain's primary (`bedrock:opus`) returns a 429 (`WAIT_AND_RETRY`, exit-11
retryable); `should_fall_back` moves to the direct-Anthropic fallback, which — with no key
on the box — raises a bare ``TypeError: Could not resolve authentication method`` at auth.
pydantic-ai's ``FallbackModel`` BARE-RERAISED that terminal ``TypeError`` (its
``fallback_on`` predicate returned False for it), discarding the collected retryable 429,
and ``classify_llm_failure`` mapped the lone ``TypeError`` to ``NEEDS_INVESTIGATION`` — a
hard INDETERMINATE — fleet-wide.

These are the held-out oracle for the fix: the chain must EXHAUST into a
``FallbackExceptionGroup`` retaining every leg, and the group must be classified by its
MOST-RECOVERABLE leg, so the retryable primary survives as ``exit 11`` retryable. Assertions
land on the observable disposition (``resolution_class`` / ``retryable``) only — never on the
tagging mechanism or any wrapper structure — so the fix is free to be implemented differently.

Everything is local and mocked at the model boundary: ZERO real Bedrock / Anthropic load.
"""

from __future__ import annotations

import pytest

pytest.importorskip("pydantic_ai")

import pydantic_ai.models as pydantic_ai_models
from pydantic_ai import Agent
from pydantic_ai.exceptions import FallbackExceptionGroup, ModelHTTPError
from pydantic_ai.models.function import AgentInfo, FunctionModel

from rebar.llm.failure import ResolutionClass, classify_llm_failure
from rebar.llm.model_classes import (
    FallbackTarget,
    _resolve_target,
    build_fallback_model,
    ensure_current_event_loop,
)

pytestmark = pytest.mark.unit

_PRIMARY = "bedrock:opus"
_FALLBACK_MODEL = "opus"
_FALLBACK_PROVIDER = "anthropic"


def _throttle_429() -> ModelHTTPError:
    """A plain Bedrock rate-limit 429 — the retryable ``WAIT_AND_RETRY`` primary failure."""
    return ModelHTTPError(
        status_code=429,
        model_name=_PRIMARY,
        body={"error": {"type": "rate_limit_error", "message": "throttled"}},
    )


def _keyless_auth_error() -> TypeError:
    """The exact failure a keyless direct-Anthropic fallback raises at auth resolution."""
    return TypeError("Could not resolve authentication method")


class _FakeSession:
    """Hands ``build_fallback_model`` a prebuilt model per resolved candidate id — the seam
    the runner fills with real provider models, here filled with deterministic stubs."""

    def __init__(self, mapping: dict) -> None:
        self._mapping = mapping

    def model_for(self, resolved: str, *, endpoint: str | None = None):
        return self._mapping[resolved]


def _raising_model(exc: BaseException) -> FunctionModel:
    def _fn(messages, info: AgentInfo):
        raise exc

    return FunctionModel(_fn)


def _throttle_then_keyless_chain():
    """The real production chain (``build_fallback_model`` → real ``should_fall_back``) whose
    primary throttles (429) and whose sole fallback is keyless (auth ``TypeError``)."""
    targets = (FallbackTarget(model=_FALLBACK_MODEL, provider=_FALLBACK_PROVIDER),)
    fallback_id = _resolve_target(_FALLBACK_MODEL, _FALLBACK_PROVIDER)
    session = _FakeSession(
        {
            _PRIMARY: _raising_model(_throttle_429()),
            fallback_id: _raising_model(_keyless_auth_error()),
        }
    )
    chain, candidates = build_fallback_model(_PRIMARY, targets, session=session)
    return chain, candidates


def test_a_bedrock_throttle_does_not_mask_as_indeterminate(monkeypatch):
    """END-TO-END through the real chain: a retryable primary throttle behind a keyless
    fallback surfaces as ``exit 11`` retryable, NOT a hard INDETERMINATE."""
    monkeypatch.setattr(pydantic_ai_models, "ALLOW_MODEL_REQUESTS", True)
    ensure_current_event_loop()  # the loop the runner pre-installs, so run_sync warns nothing
    chain, candidates = _throttle_then_keyless_chain()
    assert candidates == [_PRIMARY, f"{_FALLBACK_PROVIDER}:{_FALLBACK_MODEL}"]

    with pytest.raises(Exception) as excinfo:
        Agent(chain).run_sync("go")

    outcome = classify_llm_failure(excinfo.value)
    assert outcome.retryable is True, "a retryable throttle was masked as non-retryable"
    assert outcome.resolution_class is ResolutionClass.WAIT_AND_RETRY
    assert outcome.resolution_class is not ResolutionClass.NEEDS_INVESTIGATION


def test_a_fallback_group_is_classified_by_its_most_recoverable_leg():
    """DIRECT classifier: a group bundling [retryable 429, non-retryable auth ``TypeError``]
    classifies as the retryable leg — the fallback's failure cannot outrank the primary's."""
    group = FallbackExceptionGroup(
        "all models from FallbackModel failed",
        [_throttle_429(), _keyless_auth_error()],
    )
    outcome = classify_llm_failure(group)
    assert outcome.resolution_class is ResolutionClass.WAIT_AND_RETRY
    assert outcome.retryable is True


def test_leg_order_does_not_change_the_group_disposition():
    """The most-recoverable rule is order-independent: the retryable leg wins whether it was
    the first or the last candidate to fail."""
    reversed_group = FallbackExceptionGroup(
        "all models from FallbackModel failed",
        [_keyless_auth_error(), _throttle_429()],
    )
    outcome = classify_llm_failure(reversed_group)
    assert outcome.resolution_class is ResolutionClass.WAIT_AND_RETRY
    assert outcome.retryable is True


def test_an_all_non_retryable_group_stays_change_provider_or_model():
    """Guard against over-broadening: when NO leg is retryable, the group keeps the
    ``CHANGE_PROVIDER_OR_MODEL`` (INDETERMINATE) disposition — the whole chain is
    unavailable, which is genuinely what switching model/provider addresses."""
    credential_401 = ModelHTTPError(
        status_code=401,
        model_name=_PRIMARY,
        body={"error": {"type": "authentication_error", "message": "bad key"}},
    )
    group = FallbackExceptionGroup(
        "all models from FallbackModel failed",
        [credential_401, _keyless_auth_error()],
    )
    outcome = classify_llm_failure(group)
    assert outcome.resolution_class is ResolutionClass.CHANGE_PROVIDER_OR_MODEL
    assert outcome.retryable is False
