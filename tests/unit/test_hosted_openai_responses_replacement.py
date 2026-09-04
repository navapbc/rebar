from __future__ import annotations

import pytest

from rebar.llm.anthropic_model import _pai_model
from rebar.llm.config import LLMConfig
from rebar.llm.model_classes import CLASS_NAMES, ClassSlot, _resolve_target, primary_endpoint_for

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


def test_class_slot_endpoint_keeps_openai_chat_for_pai_model(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    slots = {name: ClassSlot(model=f"{name}-model") for name in CLASS_NAMES}
    slots["standard"] = ClassSlot(
        model="openai-chat:gpt-4o",
        endpoint="http://slot.example/v1",
    )
    monkeypatch.setattr("rebar.llm.model_classes.load_class_slots", lambda repo_root=None: slots)

    cfg = LLMConfig(model="openai-chat:gpt-4o", repo_path=".")

    assert _pai_model(cfg) == "openai-chat:gpt-4o"


def test_primary_endpoint_does_not_match_a_different_hosted_remapped_slot() -> None:
    slots = {name: ClassSlot(model=f"{name}-model") for name in CLASS_NAMES}
    slots["standard"] = ClassSlot(
        model="openai-chat:gpt-4o",
        endpoint="http://standard.example/v1",
    )

    assert primary_endpoint_for("openai-responses:gpt-4o", slots) is None
