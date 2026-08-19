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


def test_omitted_openai_provider_is_inferred_as_explicit_chat_completions() -> None:
    """A slot naming only an OpenAI model freezes the existing Chat Completions semantics."""
    mc = _mc()
    slots = mc.parse_class_slots({"frontier": {"model": "gpt-4o"}})
    assert mc.resolve_class("frontier", slots) == "openai-chat:gpt-4o"


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
    assert mc.resolve_fallback_chain("frontier", slots) == ["openai-chat:gpt-4o"]


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


# ── the integration point: the parser must be REACHABLE from real config ──────────────────
def test_load_class_slots_reads_the_model_classes_table_from_real_config(monkeypatch) -> None:
    """`parse_class_slots` takes an already-loaded mapping, which leaves open who hands it the
    real config. Without a named entry point the schema is a parser nothing calls — an orphan that
    every downstream task would have to re-invent. This pins that `load_class_slots` pulls the
    `model_classes` sub-table out of the merged `[tool.rebar.llm]` table."""
    from rebar.llm import config as llm_config

    mc = _mc()
    monkeypatch.setattr(
        llm_config,
        "_read_llm_file_table",
        lambda repo_root=None: {
            "model": "claude-opus-4-8",  # a sibling key that must be ignored
            "model_classes": {
                "standard": {"model": "us.anthropic.claude-sonnet-4-6", "provider": "bedrock"}
            },
        },
    )
    slots = mc.load_class_slots()
    assert mc.resolve_class("standard", slots) == "bedrock:us.anthropic.claude-sonnet-4-6"


def test_load_class_slots_falls_back_to_defaults_with_no_model_classes_table(monkeypatch) -> None:
    """The inherited degrade path. `_read_llm_file_table` already returns `{}` on a malformed core
    config so a broken pyproject never breaks an LLM op; a config with no `model_classes` table must
    take the same route and yield today's models rather than raising."""
    from rebar.llm import config as llm_config

    mc = _mc()
    monkeypatch.setattr(llm_config, "_read_llm_file_table", lambda repo_root=None: {})
    slots = mc.load_class_slots()
    assert mc.resolve_class("frontier", slots).endswith(_FRONTIER_TODAY)
    assert mc.resolve_class("standard", slots).endswith(_STANDARD_TODAY)
    assert mc.resolve_class("trivial", slots).endswith(_TRIVIAL_TODAY)


# ── the CLI layer: `rebar -c llm.<class>.<field>=...` ─────────────────────────────────────
def test_cli_override_beats_both_env_and_the_config_table(monkeypatch) -> None:
    """The plan states precedence CLI > env > file > default. Without this the CLI layer is
    absent from the mechanism and `rebar -c llm.frontier.model=gpt-4o` is SILENTLY IGNORED —
    precisely the silent-inference class of defect this whole epic exists to remove.

    `parse_cli_overrides` splits on the FIRST dot only, so `llm.frontier.model` arrives as the
    sub-key `"frontier.model"` under section `llm`."""
    from rebar.config import set_cli_overrides

    mc = _mc()
    monkeypatch.setenv("REBAR_LLM_FRONTIER_MODEL", "from-env")
    set_cli_overrides({"llm": {"frontier.model": "gpt-4o"}})
    try:
        slots = mc.parse_class_slots({"frontier": {"model": "from-table"}})
        assert slots["frontier"].model == "gpt-4o"
    finally:
        set_cli_overrides(None)


def test_cli_override_of_one_field_leaves_the_slot_siblings_intact() -> None:
    """Same lose-the-siblings property the env layer has: overriding one field must not clear
    the others, or a single `-c` flag silently drops a configured provider/endpoint."""
    from rebar.config import set_cli_overrides

    mc = _mc()
    set_cli_overrides({"llm": {"standard.provider": "bedrock"}})
    try:
        slots = mc.parse_class_slots(
            {"standard": {"model": "m", "endpoint": "https://e", "provider": "anthropic"}}
        )
        assert slots["standard"].provider == "bedrock"
        assert slots["standard"].model == "m"
        assert slots["standard"].endpoint == "https://e"
    finally:
        set_cli_overrides(None)


# ── reserved class names in the model-string space (172e/7761 shared foundation) ──────────
def test_a_class_name_resolves_to_the_configured_model(monkeypatch) -> None:
    """The keystone: a workflow step names a class by USING the class name as its `model:` value.
    No new `class:` key, no schema change — the closed v3 step schema keeps its shape and only its
    VALUE space gains three reserved words."""
    from rebar.llm import config as llm_config

    mc = _mc()
    monkeypatch.setattr(
        llm_config,
        "_read_llm_file_table",
        lambda repo_root=None: {
            "model_classes": {
                "standard": {"model": "us.anthropic.claude-sonnet-4-6", "provider": "bedrock"}
            }
        },
    )
    assert mc.resolve_model_string("standard") == "bedrock:us.anthropic.claude-sonnet-4-6"


def test_a_literal_model_id_passes_through_unchanged() -> None:
    """The back-compat guarantee, and the reason reserved values are safe: every existing workflow
    YAML keeps resolving byte-for-byte. If this regresses, every gate silently changes model."""
    mc = _mc()
    for literal in (
        "claude-opus-4-8",
        "anthropic:claude-opus-4-8",
        "bedrock:us.anthropic.claude-sonnet-4-6",
        "openai:gpt-4o",
    ):
        assert mc.resolve_model_string(literal) == literal


def test_every_class_name_is_reserved_not_just_standard(monkeypatch) -> None:
    """All three names must be reserved. A partial implementation that special-cased only the
    verifier's `standard` would leave `trivial`/`frontier` passing through as literal model ids —
    i.e. sent to a provider as if 'frontier' were a model name."""
    from rebar.llm import config as llm_config

    mc = _mc()
    monkeypatch.setattr(llm_config, "_read_llm_file_table", lambda repo_root=None: {})
    for name in ("trivial", "standard", "frontier"):
        out = mc.resolve_model_string(name)
        assert out != name, f"{name} was passed through as a literal model id"
        assert ":" in out, f"{name} did not resolve to a provider-qualified id"


@pytest.fixture
def _isolated_thread_loop():
    """Save/restore this thread's event loop so ``ensure_current_event_loop`` tests never leak
    loop state into the rest of the suite."""
    import asyncio
    import warnings

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", DeprecationWarning)
        try:
            saved = asyncio.get_event_loop_policy().get_event_loop()
        except RuntimeError:
            saved = None
    try:
        yield
    finally:
        asyncio.set_event_loop(saved)


def test_ensure_current_event_loop_installs_without_deprecation(_isolated_thread_loop) -> None:
    """With NO loop installed, the helper returns one and leaks NO ``DeprecationWarning`` — the
    Python 3.12 ``asyncio.get_event_loop()`` fallback that broke the provider tests (ticket
    c7d5)."""
    import asyncio
    import warnings

    mc = _mc()
    asyncio.set_event_loop(None)
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        loop = mc.ensure_current_event_loop()
    assert loop is not None and not loop.is_closed()
    # A subsequent get_event_loop (what pydantic-ai's run_sync performs) is now warning-free.
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        assert asyncio.get_event_loop() is loop


def test_ensure_current_event_loop_reuses_an_open_loop(_isolated_thread_loop) -> None:
    """An already-installed OPEN loop is REUSED (not replaced), preserving provider client loop
    affinity across a FallbackModel entry and the run it wraps."""
    import asyncio

    mc = _mc()
    installed = asyncio.new_event_loop()
    asyncio.set_event_loop(installed)
    try:
        assert mc.ensure_current_event_loop() is installed
        assert mc.ensure_current_event_loop() is installed  # idempotent
    finally:
        installed.close()


def test_ensure_current_event_loop_replaces_a_cleared_or_closed_loop(_isolated_thread_loop) -> None:
    """When the thread's loop was explicitly cleared (``set_event_loop(None)`` leaves
    ``get_event_loop`` raising) or left closed by a prior run, a fresh OPEN loop is installed
    rather than propagating the RuntimeError or handing back a dead loop."""
    import asyncio

    mc = _mc()

    # Cleared: set_event_loop(None) makes get_event_loop raise RuntimeError (set_called + None).
    asyncio.set_event_loop(None)
    fresh = mc.ensure_current_event_loop()
    assert fresh is not None and not fresh.is_closed()

    # Closed: a closed installed loop must not be reused.
    closed = asyncio.new_event_loop()
    closed.close()
    asyncio.set_event_loop(closed)
    replacement = mc.ensure_current_event_loop()
    assert replacement is not closed and not replacement.is_closed()
    replacement.close()
