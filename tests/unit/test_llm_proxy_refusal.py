"""A proxy's policy refusal is not a transient blip (bug vinyl-dubnium-squamata).

An egress proxy that answers CONNECT with a 4xx (a sandbox proxy's 403 "not in
allowed_domains", a 407 proxy-auth-required) reaches the classifier as
``ModelAPIError <- anthropic.APIConnectionError <- httpx.ProxyError <- httpcore.ProxyError``,
usually as every leg of a ``FallbackExceptionGroup``. It was classified RETRY_NOW ("Transient
connection blip — retry immediately", exit 11) though no retry can succeed.
"""

from __future__ import annotations

import pytest

anthropic = pytest.importorskip("anthropic")
httpcore = pytest.importorskip("httpcore")
httpx = pytest.importorskip("httpx")
exceptions = pytest.importorskip("pydantic_ai.exceptions")

from rebar.llm import failure  # noqa: E402
from rebar.llm.failure import ResolutionClass as RC  # noqa: E402

pytestmark = pytest.mark.unit

_REFUSAL = "403 not in this command's allowed_domains (api.anthropic.com)"
_REQ = httpx.Request("POST", "https://api.anthropic.com/v1/messages")


def _raise_chain(*excs: BaseException) -> BaseException:
    """Raise ``excs[0]``, then each next one ``from`` the previous; return the outermost."""
    caught: BaseException | None = None
    for exc in excs:
        try:
            if caught is None:
                raise exc
            raise exc from caught
        except BaseException as e:  # noqa: BLE001 — building a real __cause__ chain
            caught = e
    assert caught is not None
    return caught


def _proxy_leg(text: str = _REFUSAL, model: str = "m") -> BaseException:
    return _raise_chain(
        httpcore.ProxyError(text),
        httpx.ProxyError(text),
        anthropic.APIConnectionError(request=_REQ),
        exceptions.ModelAPIError(model, "Connection error."),
    )


def _group(*legs: BaseException) -> BaseException:
    return exceptions.FallbackExceptionGroup("All models from FallbackModel failed", list(legs))


def _surfaced(outcome: failure.LLMOutcome) -> str | None:
    diag = outcome.diagnostic
    return failure.message_for(
        outcome.resolution_class.value,
        finish_reason=diag.get("finish_reason"),
        error_type=diag.get("error_type"),
    )


def test_fallback_group_of_proxy_refusals_is_not_retryable_and_names_the_proxy():
    outcome = failure.classify_llm_failure(_group(_proxy_leg(model="a"), _proxy_leg(model="b")))
    assert outcome.resolution_class is RC.CHANGE_SETTINGS
    assert outcome.retryable is False
    assert outcome.resolution_class not in (RC.RETRY_NOW, RC.WAIT_AND_RETRY)
    assert outcome.diagnostic["error_type"] == failure.PROXY_REFUSED
    assert "allowed_domains" in outcome.diagnostic["message"]
    msg = _surfaced(outcome)
    assert msg == failure.PROXY_REFUSED_MESSAGE
    assert "proxy" in msg.lower() and "blip" in msg.lower()  # says it is NOT a blip
    assert msg != failure.message_for(RC.RETRY_NOW.value)


def test_single_proxy_refused_leg_is_not_retryable():
    outcome = failure.classify_llm_failure(_proxy_leg())
    assert outcome.resolution_class is RC.CHANGE_SETTINGS
    assert outcome.retryable is False
    assert outcome.diagnostic["error_type"] == failure.PROXY_REFUSED
    assert _surfaced(outcome) == failure.PROXY_REFUSED_MESSAGE


def test_proxy_auth_required_407_is_not_retryable():
    outcome = failure.classify_llm_failure(_group(_proxy_leg("407 Proxy Authentication Required")))
    assert outcome.resolution_class is RC.CHANGE_SETTINGS
    assert outcome.retryable is False


def test_raw_httpx_proxy_refusal_is_not_retryable():
    outcome = failure.classify_llm_failure(httpx.ProxyError(_REFUSAL))
    assert outcome.resolution_class is RC.CHANGE_SETTINGS
    assert outcome.diagnostic["error_type"] == failure.PROXY_REFUSED


def test_proxy_5xx_is_not_a_refusal_and_keeps_the_transport_disposition():
    outcome = failure.classify_llm_failure(_proxy_leg("502 Bad Gateway"))
    assert outcome.resolution_class is RC.RETRY_NOW
    assert "error_type" not in outcome.diagnostic


def test_a_retryable_leg_still_wins_over_a_proxy_refused_leg():
    throttled = exceptions.ModelHTTPError(429, "other-model", {"error": {"type": "rate_limit"}})
    outcome = failure.classify_llm_failure(_group(_proxy_leg(), throttled))
    assert outcome.resolution_class is RC.WAIT_AND_RETRY
    assert outcome.retryable is True


def test_a_group_with_a_non_proxy_hard_failure_keeps_change_provider_or_model():
    auth = exceptions.ModelHTTPError(401, "other-model", {"error": {"type": "authentication"}})
    outcome = failure.classify_llm_failure(_group(_proxy_leg(), auth))
    assert outcome.resolution_class is RC.CHANGE_PROVIDER_OR_MODEL
    assert "error_type" not in outcome.diagnostic


@pytest.mark.parametrize(
    "inner",
    [
        lambda: httpx.ConnectError("connection refused"),
        lambda: httpx.ConnectTimeout("connect timed out"),
    ],
    ids=["ConnectError", "ConnectTimeout"],
)
def test_connect_errors_keep_retry_now_alone_and_wrapped(inner):
    assert failure.classify_llm_failure(inner()).resolution_class is RC.RETRY_NOW
    wrapped = _raise_chain(
        inner(),
        anthropic.APIConnectionError(request=_REQ),
        exceptions.ModelAPIError("m", "Connection error."),
    )
    outcome = failure.classify_llm_failure(_group(wrapped, wrapped))
    assert outcome.resolution_class is RC.RETRY_NOW
    assert outcome.retryable is True


def test_plain_model_api_error_keeps_retry_now():
    outcome = failure.classify_llm_failure(exceptions.ModelAPIError("m", "Connection error."))
    assert outcome.resolution_class is RC.RETRY_NOW


def test_a_cause_cycle_terminates():
    a = exceptions.ModelAPIError("m", "Connection error.")
    b = anthropic.APIConnectionError(request=_REQ)
    a.__cause__ = b
    b.__cause__ = a
    assert failure.classify_llm_failure(a).resolution_class is RC.RETRY_NOW


def test_group_message_carries_each_distinct_proxy_text_once():
    outcome = failure.classify_llm_failure(
        _group(_proxy_leg(), _proxy_leg(), _proxy_leg("407 Proxy Authentication Required"))
    )
    msg = outcome.diagnostic["message"]
    assert msg.count("allowed_domains") == 1
    assert "407 Proxy Authentication Required" in msg


# ── where the classification is surfaced ─────────────────────────────────────────


def _raised_with_outcome() -> Exception:
    from rebar.llm.errors import LLMUnavailableError

    group = _group(_proxy_leg(model="a"), _proxy_leg(model="b"))
    err = LLMUnavailableError(f"the LLM provider call failed: {group}")
    err.outcome = failure.classify_llm_failure(group)  # type: ignore[attr-defined]
    return err


def test_raised_gate_error_prints_the_proxy_refusal_and_exits_non_retryable(capsys):
    from rebar._cli._llm_commands import _llm_error_exit_code

    assert _llm_error_exit_code(_raised_with_outcome()) == 1  # not 11 (retry)
    err = capsys.readouterr().err
    assert f"llm-degrade: CHANGE_SETTINGS — {failure.PROXY_REFUSED_MESSAGE}" in err
    assert "Transient connection blip" not in err


def test_degraded_verdict_prints_the_proxy_refusal(capsys):
    from rebar._cli._llm_commands import _disposition_exit_code

    outcome = failure.classify_llm_failure(_group(_proxy_leg()))
    result = {"verdict": "INDETERMINATE", "coverage": failure.resolution_fields(outcome)}
    assert _disposition_exit_code(result, indeterminate_code=2) != 11
    err = capsys.readouterr().err
    assert failure.PROXY_REFUSED_MESSAGE in err
    assert "Transient connection blip" not in err


def test_close_gate_failure_names_the_proxy_not_a_missing_install():
    from rebar._commands._seam import CommandError
    from rebar._commands.completion_verifier_run import _raise_close_failure

    with pytest.raises(CommandError) as info:
        _raise_close_failure(_raised_with_outcome(), "some-ticket")
    text = str(info.value)
    assert failure.PROXY_REFUSED_MESSAGE in text
    assert failure.PROXY_REFUSED_REMEDY in text
    assert failure.UNAVAILABLE_REMEDY not in text
    assert info.value.returncode == 1


def test_a_proxy_refused_primary_still_advances_the_fallback_chain():
    """A refusal is per host: a later leg on another provider host may be allowed, so the
    chain keeps trying it (the group is CHANGE_SETTINGS only when every leg was refused)."""
    from rebar.llm.model_classes import should_fall_back

    assert should_fall_back(_proxy_leg()) is True
    auth = exceptions.ModelHTTPError(401, "m", {"error": {"type": "authentication"}})
    assert should_fall_back(auth) is False  # a credential failure still surfaces


def test_generic_change_settings_message_is_unchanged_without_the_proxy_marker():
    generic = failure.message_for(RC.CHANGE_SETTINGS.value)
    assert failure.message_for(RC.CHANGE_SETTINGS.value, error_type=None) == generic
    assert failure.message_for(RC.RETRY_NOW.value, error_type=failure.PROXY_REFUSED) == (
        failure.message_for(RC.RETRY_NOW.value)
    )
