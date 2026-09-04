from __future__ import annotations

import pytest

from rebar.llm.config import LLMConfig
from rebar.llm.model_classes import _resolve_target
from rebar.llm.runner import _pai_model

pytestmark = pytest.mark.unit


def test_hosted_openai_responses_and_custom_endpoint_chat_remain_supported() -> None:
    hosted = LLMConfig(model="gpt-4o", model_provider="openai", repo_path=".")
    custom = LLMConfig(
        model="gpt-4o",
        model_provider="openai",
        base_url="http://localhost:1234/v1",
        api_key="not-needed",
        repo_path=".",
    )

    assert _resolve_target("gpt-4o", "openai") == "openai-responses:gpt-4o"
    assert _pai_model(hosted) == "openai-responses:gpt-4o"
    assert (
        _resolve_target("gpt-4o", "openai", endpoint="http://localhost:1234/v1")
        == "openai-chat:gpt-4o"
    )
    assert _pai_model(custom) == "openai-chat:gpt-4o"
