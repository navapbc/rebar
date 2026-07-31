"""The verifier paths resolve the `standard` class, not an equality heuristic (task 172e).

MEASURED defect this replaces: `resolve_verifier_model` downgraded ONLY when `cfg.model` was
EXACTLY the bare `"claude-opus-4-8"`. Provider-qualifying the SAME model
(`anthropic:claude-opus-4-8`) — or using any Bedrock id — read as "the operator chose this", so
Pass-2/Pass-4 silently inherited the frontier model, losing both the cost downgrade and (on a model
that rejects sampling params) greedy decoding.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit

_STANDARD_TODAY = "claude-sonnet-4-6"


def _no_classes(monkeypatch):
    from rebar.llm import config as llm_config

    monkeypatch.setattr(llm_config, "_read_llm_file_table", lambda repo_root=None: {})


@pytest.mark.parametrize(
    "cfg_model",
    [
        "claude-opus-4-8",
        "anthropic:claude-opus-4-8",
        "bedrock:us.anthropic.claude-opus-4-8",
        "bedrock:us.anthropic.claude-sonnet-4-6",
        "openai:gpt-4o",
    ],
)
def test_the_verifier_resolves_standard_whatever_cfg_model_is(monkeypatch, cfg_model) -> None:
    """The core fix. EVERY one of these must land on the standard class. The middle rows are the
    ones that fail today: `anthropic:claude-opus-4-8` is the SAME model as the bare default, merely
    provider-qualified, and it silently kept Pass-2 on opus."""
    from rebar.llm.config import LLMConfig
    from rebar.llm.plan_review import _verifier_cfg

    _no_classes(monkeypatch)
    out = _verifier_cfg(LLMConfig(repo_path=".", model=cfg_model))
    assert out.model.endswith(_STANDARD_TODAY), (
        f"cfg.model={cfg_model} left the verifier on {out.model}, not the standard class"
    )


def test_the_verifier_follows_a_configured_standard_class(monkeypatch) -> None:
    """The class config is what an operator now steers the verifier with — so a Bedrock standard
    class must move Pass-2 to Bedrock, which is what makes the cutover keep its two-tier shape."""
    from rebar.llm import config as llm_config
    from rebar.llm.config import LLMConfig
    from rebar.llm.plan_review import _verifier_cfg

    monkeypatch.setattr(
        llm_config,
        "_read_llm_file_table",
        lambda repo_root=None: {
            "model_classes": {
                "standard": {"model": "us.anthropic.claude-sonnet-4-6", "provider": "bedrock"}
            }
        },
    )
    out = _verifier_cfg(LLMConfig(repo_path=".", model="bedrock:us.anthropic.claude-opus-4-8"))
    assert out.model == "bedrock:us.anthropic.claude-sonnet-4-6"


def test_completion_verifier_also_resolves_standard(monkeypatch) -> None:
    """completion.py carried a SEPARATE copy of the same equality test (line ~332), so fixing only
    the plan-review path would leave the completion gate on the frontier model."""
    from rebar.llm import completion

    _no_classes(monkeypatch)
    resolved = completion._verifier_model_for_completion()
    assert resolved.endswith(_STANDARD_TODAY)


def test_nothing_configured_is_byte_identical_to_todays_verifier(monkeypatch) -> None:
    """The rollback guarantee: with no class config the verifier must still be exactly today's
    VERIFIER_DEFAULT_MODEL, so this change is invisible to every existing deployment."""
    from rebar.llm.config import VERIFIER_DEFAULT_MODEL, LLMConfig
    from rebar.llm.plan_review import _verifier_cfg

    _no_classes(monkeypatch)
    out = _verifier_cfg(LLMConfig(repo_path=".", model="claude-opus-4-8"))
    assert out.model.endswith(VERIFIER_DEFAULT_MODEL)
