"""Model-class slots: trivial / standard / frontier (task f844).

rebar's per-pass model choice was inferred from a single `cfg.model` scalar, which is why a
non-default model silently disabled the verifier downgrade. These pin the SCHEMA and RESOLVER that
replace that inference: three named class slots, each a provider target, with an optional ordered
fallback chain that is parsed and carried here but consumed later (task cc33).

Assertions are on OBSERVABLE behaviour — returned strings, raised error types, carried records —
never on internal structure, so a behaviour-preserving refactor does not break them.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit

# Today's values, which the defaults must reproduce EXACTLY so configuring nothing is a no-op.
_FRONTIER_TODAY = "claude-opus-4-8"  # config.py DEFAULT_MODEL
_STANDARD_TODAY = "claude-sonnet-4-6"  # config.py VERIFIER_DEFAULT_MODEL
_TRIVIAL_TODAY = "claude-haiku-4-5"  # plan-review.yaml model_ladder entry rung


def _mc():
    """Import at call time so a missing module fails the TEST, not collection of the file."""
    from rebar.llm import model_classes

    return model_classes


# ── happy path (the ONLY test handed to the implementer) ──────────────────────────────────
def test_a_configured_slot_resolves_to_a_provider_qualified_model() -> None:
    """The core contract: a class name plus a parsed table yields a `provider:model` string the
    runner can consume."""
    mc = _mc()
    slots = mc.parse_class_slots(
        {
            "frontier": {"model": "claude-opus-4-8", "provider": "anthropic"},
            "standard": {"model": "us.anthropic.claude-sonnet-4-6", "provider": "bedrock"},
            "trivial": {"model": "claude-haiku-4-5", "provider": "anthropic"},
        }
    )
    assert mc.resolve_class("frontier", slots) == "anthropic:claude-opus-4-8"
    assert mc.resolve_class("standard", slots) == "bedrock:us.anthropic.claude-sonnet-4-6"
    assert mc.resolve_class("trivial", slots) == "anthropic:claude-haiku-4-5"


# ── HELD OUT from the implementer ─────────────────────────────────────────────────────────
def test_nothing_configured_reproduces_todays_models_exactly() -> None:
    """HELD OUT — the rollback story, and the single most important test here. An operator who
    configures NOTHING must get byte-identical behaviour to the current release. If this drifts,
    shipping the class system silently changes which model every gate runs on."""
    mc = _mc()
    slots = mc.parse_class_slots({})
    assert mc.resolve_class("frontier", slots).endswith(_FRONTIER_TODAY)
    assert mc.resolve_class("standard", slots).endswith(_STANDARD_TODAY)
    assert mc.resolve_class("trivial", slots).endswith(_TRIVIAL_TODAY)


def test_defaults_are_bare_ids_so_no_default_pins_a_provider() -> None:
    """HELD OUT. The plan states no default may name a provider — otherwise configuring nothing
    silently locks an operator to one vendor. The defaults must resolve through inference, which
    for a `claude-*` id means anthropic, and must NOT be hard-coded as `bedrock:` or similar."""
    mc = _mc()
    slots = mc.parse_class_slots({})
    for name in ("frontier", "standard", "trivial"):
        assert mc.resolve_class(name, slots).startswith("anthropic:")


def test_omitted_provider_is_inferred_exactly_as_a_bare_id_is_today() -> None:
    """HELD OUT. A slot naming only `model` must behave identically to today's bare id, which is
    what makes the migration a no-op for existing configs."""
    from rebar.llm.config import infer_provider

    mc = _mc()
    slots = mc.parse_class_slots({"frontier": {"model": "gpt-4o"}})
    assert mc.resolve_class("frontier", slots) == f"{infer_provider('gpt-4o')}:gpt-4o"


def test_an_already_qualified_model_is_not_double_prefixed() -> None:
    """HELD OUT. `model = "anthropic:claude-opus-4-8"` already carries its provider; emitting
    `anthropic:anthropic:claude-opus-4-8` would be an unusable id the runner cannot parse."""
    mc = _mc()
    slots = mc.parse_class_slots({"frontier": {"model": "anthropic:claude-opus-4-8"}})
    assert mc.resolve_class("frontier", slots) == "anthropic:claude-opus-4-8"


def test_a_slot_carrying_an_api_key_is_rejected() -> None:
    """HELD OUT — the secrets boundary. rebar keeps API keys env-only; LiteLLM puts `api_key`
    inline in `litellm_params` and copying that would put a credential in a committed file."""
    from rebar.llm.errors import LLMConfigError

    mc = _mc()
    with pytest.raises(LLMConfigError):
        mc.parse_class_slots({"frontier": {"model": "claude-opus-4-8", "api_key": "sk-secret"}})


def test_the_rejection_message_never_echoes_the_secret() -> None:
    """HELD OUT. An error that quotes the offending value would print the credential into logs and
    CI output — turning a safety check into the leak it exists to prevent."""
    from rebar.llm.errors import LLMConfigError

    mc = _mc()
    with pytest.raises(LLMConfigError) as ei:
        mc.parse_class_slots({"frontier": {"model": "claude-opus-4-8", "api_key": "sk-leakme"}})
    assert "sk-leakme" not in str(ei.value)


def test_a_fallback_chain_is_parsed_and_carried_in_order() -> None:
    """HELD OUT. f844 parses and carries the chain; cc33 consumes it. Order is load-bearing — a
    fallback chain whose order is not preserved picks the wrong provider on failover."""
    mc = _mc()
    slots = mc.parse_class_slots(
        {
            "trivial": {
                "model": "local-haiku",
                "provider": "openai",
                "fallback": [
                    {"model": "us.anthropic.claude-haiku-4-5", "provider": "bedrock"},
                    {"model": "claude-haiku-4-5"},
                ],
            }
        }
    )
    chain = slots["trivial"].fallback
    assert len(chain) == 2
    assert chain[0].model == "us.anthropic.claude-haiku-4-5"
    assert chain[0].provider == "bedrock"
    assert chain[1].model == "claude-haiku-4-5"


def test_a_fallback_entry_carrying_an_api_key_is_rejected() -> None:
    """HELD OUT. The secrets rule applies to ENTRIES, not only to slots — a slot-only check leaves
    the obvious hiding place open, and this is exactly the case the pre-remediation plan missed."""
    from rebar.llm.errors import LLMConfigError

    mc = _mc()
    with pytest.raises(LLMConfigError):
        mc.parse_class_slots(
            {
                "frontier": {
                    "model": "claude-opus-4-8",
                    "fallback": [{"model": "gpt-4o", "api_key": "sk-secret"}],
                }
            }
        )


def test_a_nested_fallback_is_rejected() -> None:
    """HELD OUT. The chain is an ordered list, so nesting adds no expressiveness while introducing
    cycle risk and unbounded resolution depth. The plan decides it is a typed error."""
    from rebar.llm.errors import LLMConfigError

    mc = _mc()
    with pytest.raises(LLMConfigError):
        mc.parse_class_slots(
            {
                "frontier": {
                    "model": "claude-opus-4-8",
                    "fallback": [{"model": "gpt-4o", "fallback": [{"model": "gpt-4o-mini"}]}],
                }
            }
        )


def test_a_fallback_entry_omitting_provider_infers_it_like_a_slot_does() -> None:
    """HELD OUT. Entries and slots must not diverge — an entry that skipped inference would fail
    over to an unresolvable id at exactly the moment the primary is already down."""
    mc = _mc()
    slots = mc.parse_class_slots(
        {"frontier": {"model": "claude-opus-4-8", "fallback": [{"model": "gpt-4o"}]}}
    )
    assert mc.resolve_fallback_chain("frontier", slots) == ["openai:gpt-4o"]


def test_empty_fallback_and_absent_fallback_are_the_same_thing() -> None:
    """HELD OUT. The plan pins this so the empty-array case cannot be read two ways — it matters
    because `fallback = []` is how an environment says 'fail loudly, do not fail over'."""
    mc = _mc()
    empty = mc.parse_class_slots({"frontier": {"model": "claude-opus-4-8", "fallback": []}})
    absent = mc.parse_class_slots({"frontier": {"model": "claude-opus-4-8"}})
    assert list(empty["frontier"].fallback) == list(absent["frontier"].fallback) == []


def test_env_overrides_only_the_named_field_leaving_siblings_intact(monkeypatch) -> None:
    """HELD OUT — the per-field env layer. The whole reason the env form is per-FIELD rather than
    per-object is that overriding one field must not clear the others. This is the same
    lose-the-siblings failure the parent's deep-merge decision guards against."""
    mc = _mc()
    monkeypatch.setenv("REBAR_LLM_FRONTIER_MODEL", "gpt-4o")
    slots = mc.parse_class_slots(
        {"frontier": {"model": "claude-opus-4-8", "provider": "anthropic", "endpoint": "https://e"}}
    )
    assert slots["frontier"].model == "gpt-4o"
    assert slots["frontier"].provider == "anthropic"
    assert slots["frontier"].endpoint == "https://e"


def test_env_can_set_provider_and_endpoint_independently(monkeypatch) -> None:
    """HELD OUT. All nine vars must work, not just the MODEL ones — a partially-wired env layer
    would look fine in the common case and strand an operator pointing at a gateway."""
    mc = _mc()
    monkeypatch.setenv("REBAR_LLM_STANDARD_PROVIDER", "bedrock")
    monkeypatch.setenv("REBAR_LLM_TRIVIAL_ENDPOINT", "http://localhost:1234/v1")
    slots = mc.parse_class_slots({"standard": {"model": "m"}, "trivial": {"model": "t"}})
    assert slots["standard"].provider == "bedrock"
    assert slots["trivial"].endpoint == "http://localhost:1234/v1"


def test_an_unknown_class_name_is_a_typed_error_not_a_silent_default() -> None:
    """HELD OUT. Returning a default for a typo'd class would route a pass to the wrong model
    silently — the exact failure mode this whole epic exists to remove."""
    from rebar.llm.errors import LLMConfigError

    mc = _mc()
    slots = mc.parse_class_slots({})
    with pytest.raises(LLMConfigError):
        mc.resolve_class("fronteir", slots)


def test_config_py_is_not_grown_past_the_cap() -> None:
    """HELD OUT — a structural guard, not a behaviour test. config.py sits at 777 of the 800-LOC
    hard cap, which is WHY this schema lives in its own module. An implementer who ignores that and
    adds the slots to config.py breaches the cap and fails CI; catching it here is cheaper."""
    import pathlib

    cfg = pathlib.Path(__file__).resolve().parents[2] / "src" / "rebar" / "llm" / "config.py"
    limit = int(
        (pathlib.Path(__file__).resolve().parents[2] / ".github" / "module-size-limit.txt")
        .read_text()
        .strip()
    )
    loc = len(cfg.read_text().splitlines())
    assert loc <= limit, f"llm/config.py is {loc} LOC, over the {limit} cap"
