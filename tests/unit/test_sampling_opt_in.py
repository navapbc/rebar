"""Sampling parameters are opt-in on the Bedrock Converse request.

Bug tapestried-oversize-blackfly.

Measured 2026-11-11 on Bedrock Converse (us-west-2): Claude Opus 4.7 and later, Opus 5,
Sonnet 5 and the 5.5 family reject ``temperature`` 0 and ``top_p``; OpenAI GPT-5.5/6.1 and
Moonshot Kimi K3.1 reject every ``temperature`` and ``topP``. Claude 4.5/4.6, Nova Pro,
Llama 4 Maverick, Pixtral Large and DeepSeek R1 accept them. The runner must therefore send
``temperature`` only to a model measured to accept it, and must recover once from a 400 that
names a sampling parameter.

Every test drives the production path end to end — ``PydanticAIRunner.run()`` builds the real
``BedrockConverseModel`` through ``ProviderSession`` — and reads the kwargs handed to boto3's
``converse`` from an intercepting client, so no credentials, network or tokens are involved.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

pytest.importorskip("pydantic_ai")
pytest.importorskip("boto3")

from botocore.exceptions import ClientError

from rebar.llm import capabilities as caps_mod
from rebar.llm.errors import LLMConfigError, LLMUnavailableError

pytestmark = pytest.mark.unit

# Bedrock ids that 400 on `temperature=0` (measured), plus ids of the same families under the
# other inference-profile prefixes and an unreleased Claude id: none may receive sampling.
_REJECTING_IDS = [
    "us.anthropic.claude-opus-4-7",
    "us.anthropic.claude-opus-4-8",
    "us.anthropic.claude-opus-5",
    "us.anthropic.claude-sonnet-5",
    "global.anthropic.claude-sonnet-5",
    "anthropic.claude-opus-5",
    "us.anthropic.claude-sonnet-5-5",
    "us.anthropic.claude-opus-5-5",
    "us.anthropic.claude-haiku-5-5",
    "anthropic.claude-sonnet-5-5",
    "eu.anthropic.claude-sonnet-5-5",
    "apac.anthropic.claude-opus-5-5",
    "us.anthropic.claude-opus-6",
    "us.openai.gpt-5.5",
    "openai.gpt-5.5",
    "us.openai.gpt-6.1-sol",
    "us.moonshotai.kimi-k3.1",
    "moonshotai.kimi-k3.1",
]

# Bedrock ids measured to accept `temperature` 0 and 1.0 and `topP` 0.9.
_ACCEPTING_IDS = [
    "us.anthropic.claude-opus-4-6-v1",
    "us.anthropic.claude-sonnet-4-6",
    "us.anthropic.claude-haiku-4-5-20251001-v1:0",
    "us.anthropic.claude-opus-4-5-20251101-v1:0",
    "us.amazon.nova-pro-v1:0",
    "us.meta.llama4-maverick-17b-instruct-v1:0",
    "us.mistral.pixtral-large-2502-v1:0",
    "us.deepseek.r1-v1:0",
]

_CLAUDE_REJECTION = (
    "The model returned the following errors: `temperature` is deprecated for this model."
)
_CLAUDE_TOP_P_REJECTION = (
    "The model returned the following errors: `top_p` is deprecated for this model."
)
_OPENAI_REJECTION = (
    "This model doesn't support the temperature field. Remove temperature and try again."
)
_OPENAI_TOP_P_REJECTION = "This model doesn't support the topP field. Remove topP and try again."

_OK_RESPONSE = {
    "output": {"message": {"role": "assistant", "content": [{"text": "ok"}]}},
    "stopReason": "end_turn",
    "usage": {"inputTokens": 3, "outputTokens": 1, "totalTokens": 4},
    "ResponseMetadata": {"HTTPStatusCode": 200},
}


def _validation_error(message: str) -> ClientError:
    return ClientError(
        {
            "Error": {"Code": "ValidationException", "Message": message},
            "ResponseMetadata": {"HTTPStatusCode": 400},
        },
        "Converse",
    )


class _ScriptedClient:
    """A boto3 ``bedrock-runtime`` stand-in: records every ``converse`` call and answers from a
    script (an exception to raise, or a response dict); the last entry repeats."""

    meta = SimpleNamespace(region_name="us-west-2", endpoint_url="https://bedrock.invalid")

    def __init__(self, *script):
        self.script = list(script) or [_OK_RESPONSE]
        self.calls: list[dict] = []

    def converse(self, **kwargs):
        self.calls.append(kwargs)
        step = self.script[min(len(self.calls), len(self.script)) - 1]
        if isinstance(step, BaseException):
            raise step
        return step


@pytest.fixture
def fresh_sampling_memory(monkeypatch):
    """Isolate the process-lifetime record of models that rejected sampling."""
    monkeypatch.setattr(caps_mod, "_SAMPLING_REJECTED_AT_RUNTIME", set(), raising=False)


def _run(monkeypatch, tmp_path, model_id: str, client: _ScriptedClient, temperature=0.0):
    from pydantic_ai.providers.bedrock import BedrockProvider

    import rebar.llm.bedrock_model as bedrock_model
    from rebar.llm.config import LLMConfig
    from rebar.llm.runner import PydanticAIRunner, RunRequest

    monkeypatch.setenv("REBAR_GATE_ALLOW_UNGATED", "1")
    monkeypatch.setattr(
        bedrock_model,
        "build_bedrock_provider",
        lambda cfg, session=None: BedrockProvider(bedrock_client=client),
    )
    cfg = LLMConfig(
        repo_path=str(tmp_path),
        model=f"bedrock:{model_id}",
        temperature=temperature,
        bedrock_region_name="us-west-2",
        llm_retry_max_attempts=1,
    )
    return PydanticAIRunner(cfg).run(
        RunRequest(
            system_prompt="sys",
            instructions="Say ok.",
            config=cfg,
            mode="text",
            execution_mode="single_turn",
        )
    )


_SAMPLING_KEYS = ("temperature", "topP", "topK")


def _sampling_sent(call: dict) -> dict:
    inference = call.get("inferenceConfig") or {}
    extra = call.get("additionalModelRequestFields") or {}
    sent = {k: inference[k] for k in _SAMPLING_KEYS if k in inference}
    sent.update({k: extra[k] for k in ("top_k", "top_p", "temperature") if k in extra})
    return sent


@pytest.mark.usefixtures("fresh_sampling_memory")
@pytest.mark.parametrize("model_id", _REJECTING_IDS)
def test_converse_request_carries_no_sampling_for_a_model_that_rejects_it(
    monkeypatch, tmp_path, model_id
):
    client = _ScriptedClient()
    _run(monkeypatch, tmp_path, model_id, client)
    assert client.calls, "converse was never called"
    assert _sampling_sent(client.calls[0]) == {}, (
        f"{model_id} 400s on sampling parameters but the Converse request carried "
        f"{_sampling_sent(client.calls[0])}"
    )


@pytest.mark.usefixtures("fresh_sampling_memory")
@pytest.mark.parametrize("model_id", _ACCEPTING_IDS)
def test_converse_request_keeps_the_greedy_pin_for_a_model_measured_to_accept_it(
    monkeypatch, tmp_path, model_id
):
    client = _ScriptedClient()
    _run(monkeypatch, tmp_path, model_id, client)
    assert client.calls[0]["inferenceConfig"].get("temperature") == 0.0


@pytest.mark.usefixtures("fresh_sampling_memory")
def test_unset_temperature_sends_none_even_to_an_accepting_model(monkeypatch, tmp_path):
    client = _ScriptedClient()
    _run(monkeypatch, tmp_path, "us.anthropic.claude-sonnet-4-6", client, temperature=None)
    assert _sampling_sent(client.calls[0]) == {}


@pytest.mark.usefixtures("fresh_sampling_memory")
@pytest.mark.parametrize(
    "message",
    [_CLAUDE_REJECTION, _CLAUDE_TOP_P_REJECTION, _OPENAI_REJECTION, _OPENAI_TOP_P_REJECTION],
)
def test_a_sampling_rejection_is_retried_once_without_sampling_and_remembered(
    monkeypatch, tmp_path, message
):
    """A listed model that starts rejecting sampling (as Opus 4.7 did) recovers in-run: one
    retry of the same request with sampling removed, and no sampling for that model again."""
    model_id = "us.anthropic.claude-sonnet-4-6"
    client = _ScriptedClient(_validation_error(message), _OK_RESPONSE)
    _run(monkeypatch, tmp_path, model_id, client)
    assert len(client.calls) == 2, "expected exactly one retry"
    assert client.calls[0]["inferenceConfig"].get("temperature") == 0.0
    assert _sampling_sent(client.calls[1]) == {}
    # Remembered for the process lifetime: the next run never sends it.
    later = _ScriptedClient()
    _run(monkeypatch, tmp_path, model_id, later)
    assert len(later.calls) == 1
    assert _sampling_sent(later.calls[0]) == {}


@pytest.mark.usefixtures("fresh_sampling_memory")
def test_a_second_sampling_rejection_surfaces_the_actionable_config_error(monkeypatch, tmp_path):
    client = _ScriptedClient(_validation_error(_OPENAI_REJECTION))
    with pytest.raises(LLMConfigError, match="temperature"):
        _run(monkeypatch, tmp_path, "us.anthropic.claude-sonnet-4-6", client)
    assert len(client.calls) == 2, "one retry, then give up"


@pytest.mark.usefixtures("fresh_sampling_memory")
def test_an_unrelated_400_is_not_retried(monkeypatch, tmp_path):
    client = _ScriptedClient(_validation_error("Malformed input request: messages: too short"))
    with pytest.raises(LLMUnavailableError):
        _run(monkeypatch, tmp_path, "us.anthropic.claude-sonnet-4-6", client)
    assert len(client.calls) == 1


@pytest.mark.parametrize(
    "message",
    [_CLAUDE_REJECTION, _CLAUDE_TOP_P_REJECTION, _OPENAI_REJECTION, _OPENAI_TOP_P_REJECTION],
)
def test_both_rejection_wordings_translate_to_a_config_error(message):
    from pydantic_ai.exceptions import ModelHTTPError

    from rebar.llm.failure import translate_sampling_parameter_rejection

    err = ModelHTTPError(
        status_code=400,
        model_name="m",
        body={"Error": {"Code": "ValidationException", "Message": message}},
    )
    assert isinstance(translate_sampling_parameter_rejection(err, "m"), LLMConfigError)
