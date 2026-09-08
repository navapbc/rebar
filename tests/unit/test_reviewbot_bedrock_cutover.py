"""Verify the review bot's Bedrock wiring without network or container access.

The tests keep compose model-class slots equal to the provider overlay, forbid the
deprecated ``REBAR_LLM_MODEL``, match CloudWatch dimensions to invoked profiles, and
require the usage log under a persistent volume. Exact comparisons expose drift on
either side.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import tomllib
import yaml

pytestmark = pytest.mark.unit

_REPO = Path(__file__).resolve().parents[2]
_COMPOSE = _REPO / "infra" / "compose" / "docker-compose.yml"
_BEDROCK_TOML = _REPO / ".github" / "llm-providers" / "bedrock.toml"
_TERRAFORM = _REPO / "infra" / "terraform"

# The env-var name rebar reads for each model class. Spelled out rather than derived from
# rebar.llm.model_classes so this test fails on a RENAME of either side instead of following
# the rename silently — the compose file is a separate artifact that no refactor updates.
_SLOT_ENV_VARS = {
    "frontier": "REBAR_LLM_FRONTIER_MODEL",
    "standard": "REBAR_LLM_STANDARD_MODEL",
    "trivial": "REBAR_LLM_TRIVIAL_MODEL",
}

# The alarm that watches the Bedrock client-error rate. Located BY NAME across the terraform
# directory rather than by filename, so moving the resource between .tf files does not break
# this test for a reason that has nothing to do with the contract.
_ALARM_NAME = "rebar-bedrock-invoke-client-errors"


def _review_bot_service() -> dict:
    return yaml.safe_load(_COMPOSE.read_text())["services"]["review-bot"]


def _review_bot_environment() -> dict[str, str]:
    env = _review_bot_service()["environment"]
    # compose `environment:` accepts a mapping or a list of "K=V" strings; this service uses a
    # mapping, and asserting that keeps the helper honest if it ever changes shape.
    assert isinstance(env, dict), "review-bot `environment:` is expected to be a mapping"
    return {str(k): str(v) for k, v in env.items()}


def _bedrock_toml_class_models() -> dict[str, str]:
    table = tomllib.loads(_BEDROCK_TOML.read_text())["llm"]["model_classes"]
    return {cls: str(slot["model"]) for cls, slot in table.items()}


def test_compose_class_slots_are_byte_equal_to_the_bedrock_provider_overlay() -> None:
    """The production bot and the CI matrix name the SAME ids, from one source.

    Two-sided on purpose: the class SETS must match exactly and each value must be
    byte-equal, so editing either file alone fails.
    """
    env = _review_bot_environment()
    toml_models = _bedrock_toml_class_models()

    assert set(toml_models) == set(_SLOT_ENV_VARS), (
        "bedrock.toml's [llm.model_classes] and this test's slot map disagree on which classes "
        f"exist: {sorted(toml_models)} vs {sorted(_SLOT_ENV_VARS)}"
    )

    for cls, env_var in _SLOT_ENV_VARS.items():
        assert env_var in env, f"the review-bot service does not set {env_var}"
        assert env[env_var] == toml_models[cls], (
            f"{env_var} and .github/llm-providers/bedrock.toml disagree for class {cls!r}: "
            f"compose has {env[env_var]!r}, the overlay has {toml_models[cls]!r}. "
            "These are single-sourced deliberately — update BOTH."
        )


def test_compose_review_bot_sets_no_deprecated_bare_model_var() -> None:
    """The service omits the variable that would collapse the model-class split."""
    assert "REBAR_LLM_MODEL" not in _review_bot_environment(), (
        "the review-bot service sets REBAR_LLM_MODEL, which collapses the per-pass model-class "
        "split. Use the three REBAR_LLM_<CLASS>_MODEL slots instead."
    )


def test_bedrock_alarm_watches_exactly_the_model_ids_the_bot_invokes() -> None:
    """CloudWatch watches exactly the three provider profiles invoked by the bot."""
    sources = [p for p in sorted(_TERRAFORM.glob("*.tf")) if _ALARM_NAME in p.read_text()]
    assert len(sources) == 1, (
        f"expected exactly one .tf file declaring the {_ALARM_NAME} alarm, found "
        f"{[p.name for p in sources]}"
    )

    # The compose values are provider-qualified (`bedrock:<profile-id>`); CloudWatch is not, so
    # compare against the id with that qualifier removed. Split on the FIRST colon only: the
    # haiku profile id itself ends in `:0`.
    expected_model_ids = {
        model.split(":", 1)[1] if ":" in model else model
        for model in _bedrock_toml_class_models().values()
    }
    declared = set(re.findall(r"ModelId\s*=\s*\"([^\"]+)\"", sources[0].read_text()))

    assert declared == expected_model_ids, (
        f"{sources[0].name}'s ModelId dimensions and the bot's model-class ids disagree: "
        f"the alarm watches {sorted(declared)}, the bot invokes {sorted(expected_model_ids)}. "
        "An alarm on an id that receives no traffic reports healthy forever."
    )


def test_usage_log_is_enabled_and_survives_container_recreation() -> None:
    """Provider usage is enabled beneath a mount that survives container replacement."""
    env = _review_bot_environment()
    log_path = env.get("REBAR_USAGE_LOG")
    assert log_path, (
        "the review-bot service does not set REBAR_USAGE_LOG, so usage_log.record() is a no-op "
        "and there is no per-call evidence of which provider served each call."
    )

    mount_targets = [
        str(v).split(":")[1]
        for v in _review_bot_service().get("volumes", [])
        if len(str(v).split(":")) >= 2
    ]
    assert any(log_path.startswith(f"{target.rstrip('/')}/") for target in mount_targets), (
        f"REBAR_USAGE_LOG={log_path!r} is not under any of this service's volume mount targets "
        f"({mount_targets}), so an auto-deploy container recreation would discard it."
    )
