"""Contract tests for the review-bot's Bedrock cutover wiring (story eb6e).

Hermetic: no AWS, no network, no container. These pin the four things that must not drift
apart once the production review bot resolves its model classes to Bedrock inference profiles:

1. the compose class-slot VALUES against `.github/llm-providers/bedrock.toml`, so the
   production bot and the CI provider matrix name the same ids from one authoritative place;
2. the ABSENCE of `REBAR_LLM_MODEL` on that service, because its deprecation shim fans one
   value out to all three classes and would silently collapse the frontier/standard split the
   gates depend on;
3. the CloudWatch alarm's `ModelId` dimensions against those same ids — an AWS-published
   metric is dimensioned by AWS, so an alarm naming ids that no longer receive traffic reports
   healthy forever instead of failing loudly;
4. that `REBAR_USAGE_LOG` is set, and set to a path under a mounted volume — the per-call
   provider oracle is opt-in and a no-op when unset, and an auto-deploy recreates this
   container, so an unset var or an in-image path silently destroys the evidence.

Each assertion is deliberately two-sided (exact set equality, byte-equal values) so that
editing EITHER side alone fails, which is the property the story's acceptance criterion asks
for. A one-sided "compose value is in the toml" check would pass while the toml grew an id the
bot never uses.
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
    """`REBAR_LLM_MODEL` must not appear on this service.

    It is deprecated, and its compatibility shim fans a single value out to ALL THREE classes,
    collapsing the frontier/standard split: sonnet-everywhere downgrades the Pass-1 finder,
    opus-everywhere loses the Pass-2 cost downgrade and makes code-review.yaml's
    `temperature: 0` greedy pin inoperative (Bedrock opus accepts only the default
    temperature). The class slots above are the only sanctioned instrument.
    """
    assert "REBAR_LLM_MODEL" not in _review_bot_environment(), (
        "the review-bot service sets REBAR_LLM_MODEL, which collapses the per-pass model-class "
        "split. Use the three REBAR_LLM_<CLASS>_MODEL slots instead."
    )


def test_bedrock_alarm_watches_exactly_the_model_ids_the_bot_invokes() -> None:
    """The alarm's `ModelId` dimensions cover exactly the bot's three ids.

    `AWS/Bedrock` is an AWS-published namespace whose metrics are dimensioned by `ModelId`,
    and an alarm's dimensions cannot be wildcarded. So if the class slots are re-pointed and
    the alarm is not, it keeps watching ids that receive no traffic and sits healthy forever —
    a silent monitoring failure rather than a loud one. This test is that coupling.
    """
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
    """The per-call provider oracle is ON, and written where a redeploy cannot erase it.

    `usage_log.record()` is a NO-OP unless `REBAR_USAGE_LOG` names a path, so an unset var
    silently removes the only evidence that distinguishes "every call went to Bedrock" from
    "most calls went to Bedrock". That distinction is the point of the cutover:
    `ANTHROPIC_API_KEY` deliberately remains in this container so the kill switch has a working
    credential, which means "the Anthropic path is unused" must be MEASURED, not inferred from
    the key being absent.

    The path must also sit under a mounted volume: auto-deploy recreates this container on
    every deploy, so a path inside the image would lose the evidence exactly when it matters.
    """
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
