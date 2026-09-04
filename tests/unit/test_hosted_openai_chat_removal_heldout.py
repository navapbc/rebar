from __future__ import annotations

import pytest

from rebar.llm.config import LLMConfig
from rebar.llm.errors import LLMConfigError
from rebar.llm.model_classes import _resolve_target
from rebar.llm.providers import ProviderSession
from rebar.llm.runner import _pai_model

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("model", "provider"),
    [
        ("openai-chat:gpt-4o", None),
        ("openai-chat:gpt-4o", "openai"),
        ("openai:gpt-4o", "openai-chat"),
        ("gpt-4o", "openai-chat"),
    ],
)
def test_hosted_openai_chat_selection_resolves_to_responses(
    model: str, provider: str | None
) -> None:
    assert _resolve_target(model, provider) == "openai-responses:gpt-4o"


def test_hosted_openai_chat_prefix_resolves_to_responses_in_pydantic_ai_model_resolution() -> None:
    cfg = LLMConfig(model="openai-chat:gpt-4o", repo_path=".")

    assert _pai_model(cfg) == "openai-responses:gpt-4o"


@pytest.mark.parametrize(
    ("model", "provider"),
    [
        ("openai-chat:gpt-4o", None),
        ("openai-chat:gpt-4o", "openai"),
        ("openai:gpt-4o", "openai-chat"),
        ("gpt-4o", "openai-chat"),
    ],
)
def test_custom_endpoint_openai_chat_selection_stays_on_chat(
    model: str, provider: str | None
) -> None:
    assert (
        _resolve_target(model, provider, endpoint="http://localhost:1234/v1")
        == "openai-chat:gpt-4o"
    )


def test_custom_base_url_openai_chat_prefix_stays_on_chat_in_pydantic_ai_model_resolution() -> None:
    cfg = LLMConfig(
        model="openai-chat:gpt-4o",
        base_url="http://localhost:1234/v1",
        api_key="not-needed",
        repo_path=".",
    )

    assert _pai_model(cfg) == "openai-chat:gpt-4o"


def test_hosted_openai_chat_provider_factory_rejects_direct_selection_with_replacement() -> None:
    cfg = LLMConfig(model="openai-chat:gpt-4o", repo_path=".")

    with ProviderSession(cfg) as session:
        with pytest.raises(LLMConfigError) as excinfo:
            session.provider_factory("openai-chat")

    message = str(excinfo.value)
    assert "openai-chat" in message
    assert "openai-responses" in message
