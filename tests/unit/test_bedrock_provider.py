"""First-class Bedrock provider: capabilities, cache observability, and error translation (S3).

Every fact asserted here was MEASURED against real AWS (us-east-2) before it was written, not
inferred from documentation. The measurements are recorded on ticket 2932; the load-bearing
ones:

- ``us.anthropic.claude-sonnet-4-6`` caches (cache_write=4017 then cache_read=4017).
- ``us.`` AND ``global.`` opus-4-5 report cache_read=0 AND cache_write=0 while billing the full
  4029 input tokens — caching fails SILENTLY and is model-dependent, not prefix-dependent.
- ``us.anthropic.claude-opus-4-7`` + ``temperature=0`` returns 400 "temperature is deprecated
  for this model"; the identical call without it succeeds. pydantic-ai's
  ``_drop_unsupported_sampling_settings`` exists only in its Anthropic adapter, NOT its Bedrock
  one, so the direct path degrades with a warning and Bedrock hard-fails.

Most of these tests need NO ``boto3``: capability derivation, the cache-zero warning and the
error translation all work off profile stubs and plain exceptions. Only construction needs the
optional extra, and those are marked.
"""

from __future__ import annotations

import contextlib
import logging
from types import SimpleNamespace

import pytest

pytest.importorskip("pydantic_ai")

from rebar.llm.errors import LLMConfigError, LLMUnavailableError

pytestmark = pytest.mark.unit

# The model ids below are exactly those measured; see the module docstring.
_TEMP_FATAL = "us.anthropic.claude-opus-4-7"
_TEMP_OK = "us.anthropic.claude-sonnet-4-6"


@contextlib.contextmanager
def _model_via_cli(model: str):
    """Set the top-level model through the surviving CLI rung.

    The bare ``REBAR_LLM_MODEL`` env was removed and tombstoned (pre-1.0 pass #3), so a test
    that needs a specific model must drive ``LLMConfig.from_env`` through CLI or config,
    not the environment."""
    from rebar import config as _root_config

    previous = _root_config.cli_overrides_for("llm")
    _root_config.set_cli_overrides(_root_config.parse_cli_overrides([f"llm.model={model}"]))
    try:
        yield
    finally:
        _root_config.set_cli_overrides({"llm": previous} if previous else {})


def _bedrock_profile(*, variant="anthropic", json_schema=True, thinking=True):
    """A Bedrock profile stub carrying the capability FIELDS the real one exposes.

    Field VALUES mirror the real ``BedrockProvider.model_profile(...)``, which story f184 pins
    with a boto3-gated verified-fake test — so this stub cannot drift unnoticed."""
    return SimpleNamespace(
        supports_json_schema_output=json_schema,
        supports_thinking=thinking,
        bedrock_supports_prompt_caching=True,
        bedrock_supports_tool_caching=True,
        bedrock_thinking_variant=variant,
    )


# ── §A given: capability derivation for Bedrock ─────────────────────────────────────────


def test_bedrock_claude_keeps_prompt_caching_and_prompted_output():
    """The motivating defect: Bedrock-hosted Claude must get cache style "bedrock" and stay
    PROMPTED (it is still Claude). f184 already ships this; S3 must not regress it."""
    from rebar.llm.capabilities import capabilities_for

    caps = capabilities_for(SimpleNamespace(profile=_bedrock_profile()))
    assert caps.prompt_cache_style == "bedrock"
    assert caps.native_structured_output is False


def test_supports_temperature_is_withdrawn_only_for_the_measured_model():
    """Decision C. `temperature` is fatal on some Bedrock models and fine on others, so a
    blanket withdrawal would strip greedy Pass-2 determinism from the measured-good default.

    This is the contrast case that proves the exact-id table DISCRIMINATES."""
    from rebar.llm.capabilities import capabilities_for

    fatal = capabilities_for(SimpleNamespace(profile=_bedrock_profile(), model_name=_TEMP_FATAL))
    ok = capabilities_for(SimpleNamespace(profile=_bedrock_profile(), model_name=_TEMP_OK))

    assert fatal.supports_temperature is False, f"{_TEMP_FATAL} 400s on temperature (measured)"
    assert ok.supports_temperature is True, (
        f"{_TEMP_OK} accepts temperature (measured) — a blanket withdrawal would destroy the "
        "greedy Pass-2 determinism that keeps verification stable"
    )


def test_supports_temperature_defaults_true_for_everything_else():
    """The denylist must not leak: an unlisted model keeps temperature. Chosen over an
    allowlist deliberately — an unknown-but-affected model then fails LOUDLY rather than
    silently resampling verdicts.

    EXEMPLAR UPDATED (ticket 1903): this used `anthropic:claude-opus-4-8` as its "unlisted
    model" example. That model is now LISTED, because it does not accept temperature on the
    direct-Anthropic path either — OBSERVED in production, where pydantic-ai's Anthropic adapter
    logs "Sampling parameters ['temperature'] are not supported by 'claude-opus-4-8'" on
    essentially every call. So the old exemplar encoded a disproven fact. The assertion's INTENT
    and strength are unchanged; only the example moved to models that are genuinely unlisted and
    MEASURED to accept temperature."""
    from rebar.llm.capabilities import capabilities_for

    assert capabilities_for("anthropic:claude-sonnet-4-6").supports_temperature is True
    assert capabilities_for("openai:gpt-4o").supports_temperature is True


# ── §B held out from the implementer ────────────────────────────────────────────────────


def test_f184_predicate_contract_is_untouched():
    """f184 is CLOSED with a signed attestation defining its override predicates as taking a
    single ``ModelProfile``. This story adds a SEPARATE exact-id table rather than widening
    that signature — a leaf must not silently redefine a signed upstream contract."""
    import inspect

    from rebar.llm import capabilities as caps_mod

    for predicate, _overrides in caps_mod._REBAR_OVERRIDES:
        params = list(inspect.signature(predicate).parameters)
        assert len(params) == 1, (
            f"f184's override predicate {predicate.__name__} must still take exactly one "
            f"argument (the profile); got {params}"
        )


def test_capabilities_module_still_has_no_provider_name_prefix_matching():
    """f184's attested criterion. The exact-id table must use set/dict membership, never
    prefix matching — which also keeps it from speculating about unmeasured models."""
    import ast
    import pathlib

    import rebar.llm.capabilities as caps_mod

    tree = ast.parse(pathlib.Path(caps_mod.__file__).read_text())

    # ANTI-VACUITY (bug 8a5e). This guard polices an ABSENCE in ONE module, so it reads
    # identical whether the module is clean or the exact-id table has moved out from under
    # it — the failure mode that hollowed out four sibling guards. Pin the landmark: the
    # scanned module must still hold the tables this criterion is about.
    tables = {
        node.target.id if isinstance(node.target, ast.Name) else ""
        for node in ast.walk(tree)
        if isinstance(node, ast.AnnAssign)
    } | {
        t.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        for t in node.targets
        if isinstance(t, ast.Name)
    }
    missing = {"_REBAR_OVERRIDES", "_MODEL_ID_CAPABILITY_OVERRIDES"} - tables
    assert not missing, (
        f"capabilities.py no longer defines {sorted(missing)} — the exact-id capability "
        f"table this guard polices has moved, so the guard now scans a module with no "
        f"model-id matching in it and would pass unconditionally. Re-aim it at the module "
        f"that holds the table."
    )

    calls = [
        n
        for n in ast.walk(tree)
        if isinstance(n, ast.Call)
        and isinstance(n.func, ast.Attribute)
        and n.func.attr in {"startswith", "endswith"}
    ]
    assert not calls, (
        "capabilities.py must not prefix-match provider names (asserted on real calls, not on "
        f"text — a comment naming the banned pattern is fine): {[n.lineno for n in calls]}"
    )


def test_conservative_fallback_is_unchanged_when_no_model_id_is_available(caplog):
    """Adding a second table must not perturb the third path. An unrecognized input still
    degrades conservatively and still logs exactly once — never raises."""
    from rebar.llm.capabilities import capabilities_for

    with caplog.at_level(logging.WARNING):
        caps = capabilities_for(object())

    assert caps.native_structured_output is False
    assert caps.prompt_cache_style == "none"
    assert caps.supports_temperature is True, (
        "the conservative record must not withdraw temperature — that would silently disable "
        "greedy determinism for every unknown model"
    )
    assert len([r for r in caplog.records if r.levelno >= logging.WARNING]) == 1


def test_exact_id_override_beats_the_profile_shaped_one():
    """Ordering: profile-derived fields, then f184's shape overrides, THEN the exact-id table.
    A model matching both must take the exact-id value."""
    from rebar.llm.capabilities import capabilities_for

    caps = capabilities_for(SimpleNamespace(profile=_bedrock_profile(), model_name=_TEMP_FATAL))
    # _is_claude (shape) still applies...
    assert caps.native_structured_output is False
    # ...and the exact-id entry applies on top.
    assert caps.supports_temperature is False


def test_warns_when_caching_was_requested_but_both_counters_are_zero(caplog):
    """Decision B — the silent-failure guard.

    MEASURED: opus-4-5 bills the full input on every call with cache_read=0 AND cache_write=0
    and no error. The existing ``_warn_if_zeroed_usage`` CANNOT catch this: it fires on
    input_tokens==0, and here input_tokens is a healthy 40290. Without a distinct predicate an
    operator pays full price forever with no signal.

    ``input_tokens`` clears ``CACHE_MIN_PREFIX_TOKENS`` (bug 7a79): the warning now asserts that
    a CACHEABLE prompt silently failed to cache, so the fixture has to be a cacheable size. The
    sub-floor half of that contract lives in ``tests/unit/test_cache_floor_warning.py``."""
    from rebar.llm.structured_run import warn_if_cache_ineffective

    usage = {
        "input_tokens": 40290,
        "output_tokens": 4,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
    }
    with caplog.at_level(logging.WARNING):
        warn_if_cache_ineffective(
            usage, caching_requested=True, model="us.anthropic.claude-opus-4-5"
        )
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1
    assert "us.anthropic.claude-opus-4-5" in warnings[0].getMessage(), (
        "the warning must name the model so the operator can switch to a caching one"
    )


def test_no_cache_warning_when_caching_worked_or_was_not_requested(caplog):
    """Contrast case: the guard must not cry wolf."""
    from rebar.llm.structured_run import warn_if_cache_ineffective

    with caplog.at_level(logging.WARNING):
        # caching worked
        warn_if_cache_ineffective(
            {"input_tokens": 13, "cache_read_tokens": 4017, "cache_write_tokens": 0},
            caching_requested=True,
            model=_TEMP_OK,
        )
        # caching never requested (a non-caching provider)
        warn_if_cache_ineffective(
            {"input_tokens": 4029, "cache_read_tokens": 0, "cache_write_tokens": 0},
            caching_requested=False,
            model="openai:gpt-4o",
        )
    assert [r for r in caplog.records if r.levelno >= logging.WARNING] == []


def test_sampling_parameter_rejection_becomes_an_actionable_config_error():
    """The drift mitigation. An unlisted-but-affected model must not surface as an opaque
    outage — the operator has to learn WHAT TO CHANGE.

    The error text is the MEASURED one, not invented."""
    from rebar.llm.failure import translate_sampling_parameter_rejection

    # pydantic-ai surfaces this as ModelHTTPError, which carries status_code as an ATTRIBUTE —
    # model that faithfully rather than only putting "status_code: 400" in the text, or the
    # fixture tests a shape the runtime never produces.
    class _ModelHTTPErrorLike(Exception):
        status_code = 400

    via_pydantic_ai = _ModelHTTPErrorLike(
        "model_name: us.anthropic.claude-opus-4-7, body: {'Error': {'Message': \"The model "
        "returned the following errors: 'temperature' is deprecated for this model.\"}}"
    )
    # boto3 raises the same failure with ValidationException in the text and no status_code.
    via_boto3 = Exception(
        "An error occurred (ValidationException) when calling the Converse operation: The "
        "model returned the following errors: 'temperature' is deprecated for this model."
    )

    for exc in (via_pydantic_ai, via_boto3):
        err = translate_sampling_parameter_rejection(exc, _TEMP_FATAL)
        assert isinstance(err, LLMConfigError), f"not translated: {exc}"
        message = str(err)
        assert _TEMP_FATAL in message, "must name the model id the operator has to act on"
        assert "temperature" in message, "must name the offending parameter"


def test_unrelated_provider_failures_are_not_swallowed_by_the_translation():
    """Contrast case, and the reason the predicate needs all three conjuncts: a 400 that is
    NOT about a sampling parameter must keep its existing classification, or this translation
    becomes a catch-all that hides real outages."""
    from rebar.llm.failure import translate_sampling_parameter_rejection

    class _Rejection400(Exception):
        status_code = 400

    for unrelated in (
        # THE discriminating case: passes conjuncts 1 and 2 (a real 400 that names
        # `temperature`) but is a VALUE error, not a capability rejection. The operator must
        # fix the value, not add the model to the denylist — so this must NOT translate.
        # Without this case the rejection-word conjunct is never exercised: every other
        # example below already fails conjunct 1, so dropping conjunct 3 would go unnoticed.
        _Rejection400("ValidationException: temperature must be between 0.0 and 1.0, got 2.5"),
        Exception("status_code: 400, body: {'Error': {'Message': 'malformed request'}}"),
        Exception("status_code: 500, body: {'Error': {'Message': 'internal failure'}}"),
        Exception("Connection reset by peer"),
        LLMUnavailableError("the LLM provider call failed: throttled"),
    ):
        assert translate_sampling_parameter_rejection(unrelated, _TEMP_FATAL) is None, (
            f"must not translate an unrelated failure: {unrelated}"
        )


def test_failure_module_has_no_provider_name_branch():
    """The translation keys on error CONTENT, never on a provider name — the epic's criterion
    forbids provider-name branching, and any provider could reject a sampling parameter."""
    import pathlib

    import rebar.llm.failure as failure_mod

    source = pathlib.Path(failure_mod.__file__).read_text().lower()
    for banned in ('== "bedrock"', "== 'bedrock'", 'startswith("bedrock")'):
        assert banned not in source, f"provider-name branch in failure.py: {banned}"


# ── §C construction (needs the optional [bedrock] extra) ────────────────────────────────


def test_bedrock_builder_sets_exactly_the_two_parity_cache_keys():
    """PARITY, not maximalism: the direct-Anthropic path sets instructions + tool_definitions
    and leaves ``*_cache_messages`` unset, so Bedrock does the same. Enabling a third key would
    make Bedrock cache MORE than Anthropic — a different behaviour with its own cost profile,
    and story 0d76 measures the parity bar against exactly these two.

    Scoped to SINGLE_TURN, which is what this parity claim was always about: bug dd27 added a
    message-tail breakpoint to the multi-turn arm on BOTH providers (Anthropic
    ``anthropic_cache``, Bedrock ``bedrock_cache_messages``), so parity there is preserved by
    each arm using its OWN key — see ``test_agentic_message_cache.py``. The single-turn arm has
    no history to cache and is byte-unchanged, so the two-key bar still holds here exactly."""
    pytest.importorskip("boto3")
    from rebar.llm.capabilities import cache_settings_for, capabilities_for

    caps = capabilities_for(SimpleNamespace(profile=_bedrock_profile(), model_name=_TEMP_OK))
    settings = cache_settings_for(caps, execution_mode="single_turn")
    assert settings is not None
    assert settings["bedrock_cache_instructions"] is True
    assert settings["bedrock_cache_tool_definitions"] is True
    assert "bedrock_cache_messages" not in settings, (
        "message caching is deliberately NOT enabled — parity with the Anthropic path"
    )


def test_missing_bedrock_extra_raises_naming_the_install_command(monkeypatch):
    """An absent optional extra must be actionable, not an opaque ImportError from deep inside
    pydantic-ai."""
    import sys

    from rebar.llm.providers import ProviderSession

    monkeypatch.setitem(sys.modules, "pydantic_ai.providers.bedrock", None)
    from rebar.llm.config import LLMConfig

    cfg = LLMConfig(repo_path=".", model=_TEMP_OK, model_provider="bedrock")
    with ProviderSession(cfg) as session:
        with pytest.raises(LLMConfigError) as excinfo:
            session.provider_factory("bedrock")
    assert "bedrock" in str(excinfo.value)


# ── §D runner-level behaviour (the capability must actually change the request) ──────────


def _model_settings_for(model_string: str) -> dict | None:
    """The model_settings the runner assembles for ``model_string``, captured at the Agent
    boundary. Mirrors tests/unit/test_llm_temperature.py's helper — a capability record is
    only useful if it changes what leaves the process."""
    import pydantic_ai.models
    from pydantic_ai.messages import ModelResponse, TextPart
    from pydantic_ai.models.function import FunctionModel

    from rebar.llm import structured_run as structured_run_mod
    from rebar.llm.config import LLMConfig
    from rebar.llm.runner import PydanticAIRunner, RunRequest

    captured: dict = {}
    real_import = structured_run_mod._import_pydantic_ai

    def _spy_import():
        real_agent = real_import()

        class _SpyAgent(real_agent):  # type: ignore[misc,valid-type]
            def __init__(self, *a, **kw):
                captured["model_settings"] = kw.get("model_settings")
                super().__init__(*a, **kw)

        return _SpyAgent

    mp = pytest.MonkeyPatch()
    mp.setattr(structured_run_mod, "_import_pydantic_ai", _spy_import)
    pydantic_ai.models.ALLOW_MODEL_REQUESTS = False
    cfg = LLMConfig(repo_path=".", model=model_string, temperature=0.0)
    try:
        PydanticAIRunner(
            cfg, model_override=FunctionModel(lambda m, i: ModelResponse(parts=[TextPart("hi")]))
        ).run(
            RunRequest(
                system_prompt="s", instructions="i", config=cfg, reviewers=["v"], mode="text"
            )
        )
    finally:
        mp.undo()
    return captured["model_settings"]


def test_runner_omits_temperature_for_a_model_that_rejects_it():
    """The capability is only worth having if the RUNNER acts on it. Deriving
    supports_temperature=False changes nothing unless `temperature` actually stops being sent
    — MEASURED: sending it to this model returns 400 'deprecated for this model'."""
    ms = _model_settings_for(f"bedrock:{_TEMP_FATAL}") or {}
    assert "temperature" not in ms, (
        f"temperature must NOT be sent to {_TEMP_FATAL}; model_settings={ms!r}"
    )


def test_runner_still_sends_temperature_for_a_model_that_accepts_it():
    """The contrast case, and the reason a blanket Bedrock withdrawal was rejected: the
    measured-good default must KEEP temperature, or Pass-2 verification loses the greedy
    determinism that stops a re-verified finding from resampling."""
    ms = _model_settings_for(f"bedrock:{_TEMP_OK}") or {}
    assert ms.get("temperature") == 0.0, (
        f"temperature must still be sent to {_TEMP_OK}; model_settings={ms!r}"
    )


def test_translation_names_both_remedies_not_just_the_problem():
    """An actionable error names the FIX. Telling an operator a parameter was rejected without
    saying what to change leaves them exactly as stuck as an opaque 400 would."""
    from rebar.llm.failure import translate_sampling_parameter_rejection

    class _E(Exception):
        status_code = 400

    err = translate_sampling_parameter_rejection(
        _E("'temperature' is deprecated for this model"), _TEMP_FATAL
    )
    assert err is not None
    message = str(err)
    assert "_MODEL_ID_CAPABILITY_OVERRIDES" in message, "remedy 1: the override table"
    assert "TEMPERATURE" in message.upper(), "remedy 2: unset the temperature config"


def test_unrelated_provider_failure_still_surfaces_as_llm_unavailable_at_the_runner():
    """End of the contrast: the translation must not change how an UNRELATED failure is
    classified. Asserted through the runner, not just the helper, because that classification
    is what a caller actually sees."""
    import pydantic_ai.models
    from pydantic_ai.models.function import FunctionModel

    from rebar.llm.config import LLMConfig
    from rebar.llm.runner import PydanticAIRunner, RunRequest

    def _boom(messages, info):
        raise RuntimeError("status_code: 500, body: {'Error': {'Message': 'internal failure'}}")

    pydantic_ai.models.ALLOW_MODEL_REQUESTS = False
    cfg = LLMConfig(repo_path=".", model=f"bedrock:{_TEMP_OK}")
    with pytest.raises(LLMUnavailableError):
        PydanticAIRunner(cfg, model_override=FunctionModel(_boom)).run(
            RunRequest(
                system_prompt="s", instructions="i", config=cfg, reviewers=["v"], mode="text"
            )
        )


# ── 281f: the inert REBAR_LLM_BEDROCK_MODEL_ID knob is REMOVED ────────────────────────────
def test_bedrock_model_id_config_field_is_gone(monkeypatch) -> None:
    """281f (happy path): `REBAR_LLM_BEDROCK_MODEL_ID` was parsed onto `LLMConfig` but read by
    NOTHING, so an operator who set it got silence — no model change, no warning, no error.

    The knob is deleted rather than wired: `cfg.model` ALREADY carries the Bedrock id as the
    provider-prefixed `bedrock:<inference-profile-id>` string (the path S3's tests exercise), so
    wiring a second source would require precedence rules for two ways to say one value. This
    asserts the ATTRIBUTE is gone, so a future reintroduction of an unread field fails here."""
    from rebar.llm.config import LLMConfig

    monkeypatch.setenv("REBAR_LLM_BEDROCK_MODEL_ID", "us.anthropic.claude-opus-4-8")
    cfg = LLMConfig.from_env()
    assert not hasattr(cfg, "bedrock_model_id"), (
        "LLMConfig still exposes `bedrock_model_id`; setting REBAR_LLM_BEDROCK_MODEL_ID must "
        "have no config surface at all, since nothing reads it"
    )


def test_bedrock_provider_prefixed_model_still_resolves(monkeypatch) -> None:
    """281f collateral invariant (HELD OUT): removing the inert knob must not disturb the
    WORKING path. The Bedrock model id travels on `cfg.model` as `bedrock:<profile-id>`, and
    `infer_provider` splits the prefix. If a fix over-deletes and takes the real path with it,
    this bites."""
    from rebar.llm.config import LLMConfig, infer_provider

    with _model_via_cli("bedrock:us.anthropic.claude-sonnet-4-6"):
        cfg = LLMConfig.from_env()
    assert cfg.model == "bedrock:us.anthropic.claude-sonnet-4-6"
    assert infer_provider(cfg.model) == "bedrock"


def test_bedrock_region_knob_is_untouched(monkeypatch) -> None:
    """281f collateral invariant (HELD OUT): `REBAR_LLM_BEDROCK_REGION` is a SIBLING setting that
    IS genuinely read (`build_bedrock_provider` threads `cfg.bedrock_region_name` into
    `BedrockProvider`). Deleting the inert model-id knob must not delete its live neighbour —
    an easy over-deletion given the adjacent lines. This matters more since a574: a missing
    region raises NoRegionError at client construction."""
    from rebar.llm.config import LLMConfig

    monkeypatch.setenv("REBAR_LLM_BEDROCK_REGION", "us-east-1")
    cfg = LLMConfig.from_env()
    assert cfg.bedrock_region_name == "us-east-1"


def test_env_registry_no_longer_documents_the_removed_knob() -> None:
    """281f (HELD OUT): docs/env-vars.md is GENERATED from the env reads in src/rebar and a CI
    drift gate fails the build when it is stale. Removing the config read without regenerating
    leaves the docs advertising a knob that no longer exists — the same 'documented but inert'
    defect this ticket exists to remove, just inverted."""
    from pathlib import Path

    registry = Path(__file__).resolve().parents[2] / "docs" / "env-vars.md"
    text = registry.read_text(encoding="utf-8")
    assert "REBAR_LLM_BEDROCK_MODEL_ID" not in text, (
        "docs/env-vars.md still lists REBAR_LLM_BEDROCK_MODEL_ID after its config read was removed"
    )
    assert "REBAR_LLM_BEDROCK_REGION" in text, (
        "the live sibling REBAR_LLM_BEDROCK_REGION disappeared from the registry — over-deletion"
    )


# ── 1903: the supports_temperature override table covers the whole affected family ─────────
def test_default_model_opus_4_8_withdraws_temperature() -> None:
    """1903 (happy path): `us.anthropic.claude-opus-4-8` REJECTS an explicit temperature, and it
    is rebar's DEFAULT_MODEL — so it is the most likely Bedrock Pass-1 model and the one the
    override table most needs to cover. MEASURED (account 896586841071, us-east-1):
    converse(temperature=0.0) -> ValidationException "`temperature` is deprecated for this
    model"; the identical call with no temperature succeeds.

    NOTE the PROVIDER-QUALIFIED form: `_model_id_of` returns None for a bare id with no colon,
    so a bare-string assertion would pass vacuously against the conservative record rather than
    exercising the override table. Production always passes `provider:model` or a model object."""
    from rebar.llm.capabilities import capabilities_for

    assert capabilities_for("bedrock:us.anthropic.claude-opus-4-8").supports_temperature is False


def test_global_prefix_opus_4_8_also_withdraws_temperature() -> None:
    """HELD OUT. The table keys on the FULL model id, so the `global.` twin is a separate entry
    and is unguarded unless listed explicitly. MEASURED: global.anthropic.claude-opus-4-8 +
    temperature=0.0 -> the same 400."""
    from rebar.llm.capabilities import capabilities_for

    caps = capabilities_for("bedrock:global.anthropic.claude-opus-4-8")
    assert caps.supports_temperature is False


def test_both_opus_4_7_prefixes_withdraw_temperature() -> None:
    """HELD OUT. Only the `us.` opus-4-7 entry existed before this change; the `global.` twin was
    unguarded. MEASURED: both 400 on temperature=0.0."""
    from rebar.llm.capabilities import capabilities_for

    for mid in ("bedrock:us.anthropic.claude-opus-4-7", "bedrock:global.anthropic.claude-opus-4-7"):
        assert capabilities_for(mid).supports_temperature is False, mid


def test_sonnet_retains_temperature_support() -> None:
    """HELD OUT — the over-application guard, and the most important test here. Sonnet ACCEPTS
    temperature (MEASURED: us. and global. claude-sonnet-4-6 both succeed at temperature=0.0 and
    0.5). A blanket family-level withdrawal would silently strip Pass-2's greedy determinism from
    the model that actually supports it, which is a worse defect than the one being fixed."""
    from rebar.llm.capabilities import capabilities_for

    for mid in (
        "bedrock:us.anthropic.claude-sonnet-4-6",
        "bedrock:global.anthropic.claude-sonnet-4-6",
    ):
        assert capabilities_for(mid).supports_temperature is True, mid


def test_direct_anthropic_opus_also_withdraws_temperature() -> None:
    """1903 (extension): the SAME model rejects temperature on the DIRECT-ANTHROPIC path too.

    OBSERVED IN PRODUCTION on the code-review bot: pydantic-ai's Anthropic adapter logs
    "Sampling parameters ['temperature'] are not supported by 'claude-opus-4-8'. These settings
    will be ignored." on essentially every call. Its Bedrock adapter has no equivalent drop, so
    the same model 400s there and only warns here — one defect, two symptoms.

    Withdrawing the parameter is wire-identical to having it dropped downstream, but it removes
    the per-call warning AND the false belief that a pass pinning temperature=0 is decoding
    greedily. That matters: code review pins temperature=0 on Pass-2 verify so re-running a
    finding cannot resample its verdict, and code review has no verifier downgrade, so every one
    of its passes runs on this model."""
    from rebar.llm.capabilities import capabilities_for

    for mid in ("anthropic:claude-opus-4-8", "anthropic:claude-opus-4-7"):
        assert capabilities_for(mid).supports_temperature is False, mid
    # sonnet is unaffected on this path too — the guard must not over-apply
    assert capabilities_for("anthropic:claude-sonnet-4-6").supports_temperature is True


# ── a574: a missing region must fail as a TYPED, actionable rebar error ────────────────────
# MEASURED in compose-review-bot-1 (896586841071): no AWS_REGION, no AWS_DEFAULT_REGION, and
# boto3.session.Session().region_name is None, so BedrockProvider raised a bare boto3
# NoRegionError from deep inside construction. Every earlier probe in this epic passed
# region_name="us-east-1" EXPLICITLY, which is why ambient resolution was never exercised.
def _stub_bedrock_provider(monkeypatch):
    """Replace BedrockProvider with a recorder so these tests never build a real AWS client."""
    import pydantic_ai.providers.bedrock as bedrock_mod

    seen = {}

    class _Stub:
        def __init__(self, *, region_name=None, **kw):
            seen["region_name"] = region_name
            seen.update(kw)

    monkeypatch.setattr(bedrock_mod, "BedrockProvider", _Stub)
    return seen


def _no_ambient_region(monkeypatch):
    """Reproduce the container: no AWS_* region env and boto3 resolving nothing."""
    import boto3

    for var in ("AWS_REGION", "AWS_DEFAULT_REGION", "AWS_PROFILE"):
        monkeypatch.delenv(var, raising=False)

    class _NoRegionSession:
        region_name = None

        def __init__(self, *a, **kw):
            pass

    monkeypatch.setattr(boto3.session, "Session", _NoRegionSession)


def test_missing_region_raises_a_typed_error_naming_the_setting(monkeypatch) -> None:
    """a574 AC3. Asserted POSITIVELY — the error IS an LLMConfigError and its message NAMES the
    knob an operator has to set. The negative form ('not a NoRegionError') passes vacuously: any
    unrelated exception, or a boto3 that stopped raising, would satisfy it while leaving the
    operator with the same unactionable failure."""
    from rebar.llm.bedrock_model import build_bedrock_provider
    from rebar.llm.config import LLMConfig
    from rebar.llm.errors import LLMConfigError

    _stub_bedrock_provider(monkeypatch)
    _no_ambient_region(monkeypatch)
    cfg = LLMConfig(repo_path=".", model="bedrock:us.anthropic.claude-sonnet-4-6")

    with pytest.raises(LLMConfigError) as ei:
        build_bedrock_provider(cfg)
    assert "REBAR_LLM_BEDROCK_REGION" in str(ei.value)


def test_an_explicit_rebar_region_still_builds(monkeypatch) -> None:
    """Guard against an over-eager check: the knob being SET is the fixed configuration, so it
    must not trip the new error. Without this, 'always raise' would satisfy the test above.
    The region now reaches the boto3 SESSION (the client is built here since bug 61d8), so
    that is where the oracle looks."""
    from rebar.llm.bedrock_model import build_bedrock_provider
    from rebar.llm.config import LLMConfig

    _stub_bedrock_provider(monkeypatch)
    for var in ("AWS_REGION", "AWS_DEFAULT_REGION", "AWS_PROFILE"):
        monkeypatch.delenv(var, raising=False)
    seen = _spy_boto_session(monkeypatch)
    cfg = LLMConfig(
        repo_path=".",
        model="bedrock:us.anthropic.claude-sonnet-4-6",
        bedrock_region_name="us-east-1",
    )

    build_bedrock_provider(cfg)
    assert seen["session_kwargs"].get("region_name") == "us-east-1"


def test_an_ambient_boto3_region_is_still_honoured(monkeypatch) -> None:
    """The local-dev / AWS_PROFILE path must keep working. rebar deliberately does NOT invent a
    default region, so when boto3 CAN resolve one ambiently the provider is built and rebar passes
    None through — letting boto3 use what it resolved. An implementation that demanded the rebar
    knob unconditionally would break every developer using AWS_DEFAULT_REGION or a profile."""
    from rebar.llm.bedrock_model import build_bedrock_provider
    from rebar.llm.config import LLMConfig

    provider_seen = _stub_bedrock_provider(monkeypatch)
    seen = _spy_boto_session(monkeypatch, ambient_region="eu-west-1")
    cfg = LLMConfig(repo_path=".", model="bedrock:us.anthropic.claude-sonnet-4-6")

    build_bedrock_provider(cfg)
    assert provider_seen.get("bedrock_client") is seen["sentinel"]  # built, not rejected


def test_botocore_still_ignores_aws_region_but_rebar_now_closes_the_trap(
    monkeypatch, tmp_path
) -> None:
    """8274 (rewrites the 4e71 oracle, which pinned the old raise). Two claims in one test:

    1. The MEASURED botocore quirk still holds — with AWS_DEFAULT_REGION genuinely absent
       (deleted, not set empty) and AWS_REGION set, the REAL boto3 resolution chain resolves
       nothing. If a future botocore starts honouring AWS_REGION this arm goes RED, the signal
       to re-measure and simplify rebar's own chain.
    2. rebar no longer inherits that quirk: `build_bedrock_provider` resolves AWS_REGION in
       its OWN chain and passes it explicitly as `region_name=`, so the build now SUCCEEDS
       where it used to raise a typed LLMConfigError (the 4e71-era behaviour).

    Profile config files are pointed at nonexistent paths so an ambient ~/.aws/config on a
    developer machine cannot supply a region and mask either claim.
    """
    import boto3

    from rebar.llm.bedrock_model import build_bedrock_provider
    from rebar.llm.config import LLMConfig

    _stub_bedrock_provider(monkeypatch)
    monkeypatch.delenv("AWS_DEFAULT_REGION", raising=False)
    monkeypatch.delenv("AWS_PROFILE", raising=False)
    monkeypatch.setenv("AWS_REGION", "us-east-1")
    monkeypatch.setenv("AWS_CONFIG_FILE", str(tmp_path / "absent-config"))
    monkeypatch.setenv("AWS_SHARED_CREDENTIALS_FILE", str(tmp_path / "absent-credentials"))

    # claim 1: AWS_REGION alone is still not a region source for botocore itself
    assert boto3.session.Session().region_name is None

    # claim 2: rebar's own chain consumes AWS_REGION and the session receives it explicitly
    seen = _spy_boto_session(monkeypatch)
    cfg = LLMConfig(repo_path=".", model="bedrock:us.anthropic.claude-sonnet-4-6")
    build_bedrock_provider(cfg)
    assert seen["session_kwargs"].get("region_name") == "us-east-1"


# ── 8274: rebar's OWN region chain — AWS_REGION honoured, precedence pinned ─────────────────
# botocore ignores AWS_REGION (measured above), the standard variable operators actually set;
# rebar resolves the region itself and hands it to the session explicitly so the quirk is moot.


def _strip_region_sources(monkeypatch) -> None:
    for var in ("AWS_REGION", "AWS_DEFAULT_REGION", "AWS_PROFILE"):
        monkeypatch.delenv(var, raising=False)


def test_region_resolution_precedence_each_source_beats_the_ones_after_it(monkeypatch) -> None:
    """8274 AC2. All three sources set to DISTINCT regions, then stripped front-to-back, pins
    the full order: REBAR_LLM_BEDROCK_REGION > AWS_DEFAULT_REGION > AWS_REGION > nothing.
    Exercises the pure resolver directly — the seam the provenance record reads too — so a
    reordered or dropped arm cannot hide behind the builder's session plumbing."""
    from rebar.llm.bedrock_model import resolve_bedrock_region

    _strip_region_sources(monkeypatch)
    monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-west-1")
    monkeypatch.setenv("AWS_REGION", "us-west-2")

    assert resolve_bedrock_region("us-east-1") == ("us-east-1", "REBAR_LLM_BEDROCK_REGION")
    assert resolve_bedrock_region(None) == ("eu-west-1", "AWS_DEFAULT_REGION")
    monkeypatch.delenv("AWS_DEFAULT_REGION")
    assert resolve_bedrock_region(None) == ("us-west-2", "AWS_REGION")
    monkeypatch.delenv("AWS_REGION")
    assert resolve_bedrock_region(None) == (None, None)


def test_empty_string_env_regions_are_treated_as_unset(monkeypatch) -> None:
    """Botocore parity: an empty AWS_DEFAULT_REGION does not resolve a region there, so an
    empty value must fall through rebar's chain too, not resolve as ''."""
    from rebar.llm.bedrock_model import resolve_bedrock_region

    _strip_region_sources(monkeypatch)
    monkeypatch.setenv("AWS_DEFAULT_REGION", "")
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    assert resolve_bedrock_region(None) == ("us-west-2", "AWS_REGION")
    monkeypatch.setenv("AWS_REGION", "")
    assert resolve_bedrock_region("") == (None, None)


# ── cda8: truthful region_source — the configured knob's ORIGIN labels the record ───────────
# The knob arm used to label EVERY configured value "REBAR_LLM_BEDROCK_REGION", so a
# rebar.toml pin was mislabeled as the env var in signed provenance. The label now rides the
# SAME resolution pass that produced the value (LLMConfig.from_env -> bedrock_region_source),
# threaded here as `configured_source` — never re-derived, so record and runtime agree.


@contextlib.contextmanager
def _bedrock_region_via_cli(region: str):
    """Set the Bedrock region through the CLI rung (`rebar -c llm.bedrock_region_name=…`)."""
    from rebar import config as _root_config

    previous = _root_config.cli_overrides_for("llm")
    _root_config.set_cli_overrides(
        _root_config.parse_cli_overrides([f"llm.bedrock_region_name={region}"])
    )
    try:
        yield
    finally:
        _root_config.set_cli_overrides({"llm": previous} if previous else {})


def test_configured_source_labels_the_knob_arm_verbatim(monkeypatch) -> None:
    """cda8 AC1/AC3 at the resolver seam: the threaded origin is returned VERBATIM as the
    source — a repo-config pin is labeled repo-config, a CLI value cli — while the resolved
    VALUE is byte-identical to the unlabeled call."""
    from rebar.llm.bedrock_model import resolve_bedrock_region

    _strip_region_sources(monkeypatch)
    assert resolve_bedrock_region("us-east-1", configured_source="repo-config") == (
        "us-east-1",
        "repo-config",
    )
    assert resolve_bedrock_region("us-east-1", configured_source="cli") == ("us-east-1", "cli")


def test_sourceless_configured_region_keeps_the_env_var_label(monkeypatch) -> None:
    """Compatibility: a caller that constructs LLMConfig directly has no origin to thread, so
    the knob arm keeps its historical `REBAR_LLM_BEDROCK_REGION` label rather than guessing."""
    from rebar.llm.bedrock_model import resolve_bedrock_region

    _strip_region_sources(monkeypatch)
    assert resolve_bedrock_region("us-east-1") == ("us-east-1", "REBAR_LLM_BEDROCK_REGION")


def test_configured_source_never_leaks_into_env_chain_arms(monkeypatch) -> None:
    """When the knob is UNSET the threaded origin is meaningless and must not relabel (or
    reorder) the AWS_DEFAULT_REGION > AWS_REGION > nothing tail of the chain."""
    from rebar.llm.bedrock_model import resolve_bedrock_region

    _strip_region_sources(monkeypatch)
    monkeypatch.setenv("AWS_DEFAULT_REGION", "eu-west-1")
    assert resolve_bedrock_region(None, configured_source="repo-config") == (
        "eu-west-1",
        "AWS_DEFAULT_REGION",
    )
    monkeypatch.delenv("AWS_DEFAULT_REGION")
    assert resolve_bedrock_region("", configured_source="repo-config") == (None, None)


def test_from_env_precedence_and_label_pairs_for_every_source_combination(monkeypatch) -> None:
    """cda8 AC4: the precedence+label pair for every source combination, stripped
    front-to-back. Precedence is UNCHANGED (CLI > REBAR_LLM_BEDROCK_REGION > config file) and
    the label always names the layer that actually won."""
    from rebar.llm import config as llm_config

    _strip_region_sources(monkeypatch)
    monkeypatch.delenv("REBAR_LLM_BEDROCK_REGION", raising=False)
    monkeypatch.setattr(
        llm_config,
        "_read_llm_file_table",
        lambda repo_root=None: {"bedrock_region_name": "eu-central-1"},
    )

    monkeypatch.setenv("REBAR_LLM_BEDROCK_REGION", "us-east-1")
    with _bedrock_region_via_cli("ap-southeast-2"):
        cfg = llm_config.LLMConfig.from_env(repo_root=".")
    assert (cfg.bedrock_region_name, cfg.bedrock_region_source) == ("ap-southeast-2", "cli")

    cfg = llm_config.LLMConfig.from_env(repo_root=".")
    assert (cfg.bedrock_region_name, cfg.bedrock_region_source) == (
        "us-east-1",
        "REBAR_LLM_BEDROCK_REGION",
    )

    monkeypatch.delenv("REBAR_LLM_BEDROCK_REGION")
    cfg = llm_config.LLMConfig.from_env(repo_root=".")
    assert (cfg.bedrock_region_name, cfg.bedrock_region_source) == ("eu-central-1", "repo-config")


def test_from_env_unconfigured_region_carries_no_source_label(monkeypatch) -> None:
    """No layer set the knob -> value None AND source None: nothing to label, nothing guessed."""
    from rebar.llm import config as llm_config

    _strip_region_sources(monkeypatch)
    monkeypatch.delenv("REBAR_LLM_BEDROCK_REGION", raising=False)
    monkeypatch.setattr(llm_config, "_read_llm_file_table", lambda repo_root=None: {})
    cfg = llm_config.LLMConfig.from_env(repo_root=".")
    assert (cfg.bedrock_region_name, cfg.bedrock_region_source) == (None, None)


def test_aws_region_alone_builds_and_reaches_the_session_explicitly(monkeypatch) -> None:
    """8274 AC1 (the symptom itself). Only AWS_REGION is set — the shell shape operators
    actually have — and the build must succeed with THAT region passed explicitly to the
    boto3 session (never left to botocore, which ignores AWS_REGION)."""
    from rebar.llm.bedrock_model import build_bedrock_provider
    from rebar.llm.config import LLMConfig

    provider_seen = _stub_bedrock_provider(monkeypatch)
    _strip_region_sources(monkeypatch)
    monkeypatch.setenv("AWS_REGION", "us-west-2")
    seen = _spy_boto_session(monkeypatch)
    cfg = LLMConfig(repo_path=".", model="bedrock:us.anthropic.claude-sonnet-4-6")

    build_bedrock_provider(cfg)

    assert seen["session_kwargs"].get("region_name") == "us-west-2"
    assert provider_seen.get("bedrock_client") is seen["sentinel"]


def test_no_region_error_names_the_full_chain_and_drops_the_stale_warning(monkeypatch) -> None:
    """8274 AC4. The no-region-anywhere error must name every source in rebar's chain — an
    operator can fix it by setting ANY of the three — and the old 'AWS_REGION alone does NOT
    resolve a region' guidance must be gone: rebar now consults AWS_REGION itself, so that
    paragraph would send operators away from a variable that works."""
    from rebar.llm.bedrock_model import build_bedrock_provider
    from rebar.llm.config import LLMConfig
    from rebar.llm.errors import LLMConfigError

    _stub_bedrock_provider(monkeypatch)
    _no_ambient_region(monkeypatch)
    cfg = LLMConfig(repo_path=".", model="bedrock:us.anthropic.claude-sonnet-4-6")

    with pytest.raises(LLMConfigError) as ei:
        build_bedrock_provider(cfg)
    text = str(ei.value)
    for source in ("REBAR_LLM_BEDROCK_REGION", "AWS_DEFAULT_REGION", "AWS_REGION"):
        assert source in text, f"the error no longer names {source} as a region source"
    assert "does NOT resolve" not in text, (
        "the stale AWS_REGION-does-not-work guidance survived — AWS_REGION is now a live "
        "source in rebar's own chain, so this paragraph misdirects operators"
    )
    # IMDS note stays: credential discovery and region discovery are independent
    assert "IMDS" in text


# ── 61d8: llm_retry_max_attempts / timeout_s must reach the botocore client Config ──────────
# The inert-config defect: both knobs were documented but had NO read site on the Bedrock
# path — `build_bedrock_provider` ended at `BedrockProvider(region_name=region)` and the
# client ran on botocore's stock defaults. These oracles are BEHAVIOURAL: a spy boto3
# session records what client was constructed and with which botocore Config, so a revert
# to the bare `BedrockProvider(region_name=region)` (which never builds a client through
# the session) turns them RED. They are NOT satisfiable by the Anthropic path.


def _spy_boto_session(monkeypatch, *, ambient_region=None):
    """Monkeypatch ``boto3.session.Session`` with a spy that records client construction.

    Records the ``region_name`` passed to the Session, the service name and the
    ``config``/kwargs passed to ``.client``, and returns a sentinel client object so the
    test can assert the provider received exactly that client."""
    import boto3

    seen: dict = {}
    sentinel = object()

    class _SpySession:
        def __init__(self, *a, **kw):
            seen["session_kwargs"] = kw
            self.region_name = kw.get("region_name") or ambient_region

        def client(self, service_name, **kw):
            seen["service_name"] = service_name
            seen["client_kwargs"] = kw
            return sentinel

    monkeypatch.setattr(boto3.session, "Session", _SpySession)
    seen["sentinel"] = sentinel
    return seen


def test_configured_retry_and_timeout_reach_the_botocore_client_config(monkeypatch) -> None:
    """61d8 AC1+AC3. The two documented knobs — `llm_retry_max_attempts`
    (REBAR_LLM_RETRY_MAX_ATTEMPTS) and the timeout (`timeout_s`, REBAR_LLM_TIMEOUT) — must
    arrive in the botocore `Config` the Bedrock runtime client is constructed with:
    retries as total attempts in "adaptive" mode, the timeout as both read and connect
    timeout (the Anthropic path applies one `httpx.Timeout(timeout_s)` to every phase).

    Values are deliberately non-default so a client built on stock defaults cannot pass."""
    from botocore.config import Config as BotoConfig

    from rebar.llm.bedrock_model import build_bedrock_provider
    from rebar.llm.config import LLMConfig

    provider_seen = _stub_bedrock_provider(monkeypatch)
    session_seen = _spy_boto_session(monkeypatch)
    cfg = LLMConfig(
        repo_path=".",
        model="bedrock:us.anthropic.claude-sonnet-4-6",
        bedrock_region_name="us-east-1",
        llm_retry_max_attempts=7,
        timeout_s=123,
    )

    build_bedrock_provider(cfg)

    assert session_seen.get("service_name") == "bedrock-runtime", (
        "no bedrock-runtime client was constructed through the boto3 session — the builder "
        "has reverted to the inert BedrockProvider(region_name=...) form (bug 61d8)"
    )
    boto_config = session_seen["client_kwargs"].get("config")
    assert isinstance(boto_config, BotoConfig)
    assert boto_config.retries == {"max_attempts": 7, "mode": "adaptive"}
    assert boto_config.read_timeout == 123.0
    assert boto_config.connect_timeout == 123.0
    # the provider must be built ON that client, not on a second unconfigured one
    assert provider_seen.get("bedrock_client") is session_seen["sentinel"]


def test_retry_attempts_below_one_clamp_to_a_single_attempt(monkeypatch) -> None:
    """Parity with the Anthropic envelope's `max(1, ...)`: `llm_retry_max_attempts <= 1`
    means fail-fast (one attempt, zero retries), never a botocore ValidationError on 0."""
    from rebar.llm.bedrock_model import build_bedrock_provider
    from rebar.llm.config import LLMConfig

    _stub_bedrock_provider(monkeypatch)
    session_seen = _spy_boto_session(monkeypatch)
    cfg = LLMConfig(
        repo_path=".",
        model="bedrock:us.anthropic.claude-sonnet-4-6",
        bedrock_region_name="us-east-1",
        llm_retry_max_attempts=0,
    )

    build_bedrock_provider(cfg)

    assert session_seen["client_kwargs"]["config"].retries["max_attempts"] == 1


# ── adb6: the repo-config rung — [llm] bedrock_region_name reaches region resolution ────────
# The fresh-shell failure (bug adb6-4762-90f6-4e30): rebar.toml pins region-scoped `us.*`
# inference profiles but not the region they are served from, so a shell exporting only
# AWS_PROFILE (whose profile carries no region) fails every gated LLM op with the a574
# LLMConfigError. The fix pins `bedrock_region_name` in the project [llm] table; these pin
# (a) the table rung actually resolving, and (b) env keeping precedence over it.


def _region_pinned_project(tmp_path):
    """A repo root whose discovered rebar.toml pins the Bedrock region beside the model."""
    proj = tmp_path / "proj"
    proj.mkdir()
    (proj / ".git").mkdir()
    (proj / "rebar.toml").write_text(
        '[llm]\nmodel = "bedrock:us.anthropic.claude-sonnet-4-6"\n'
        'bedrock_region_name = "us-east-1"\n',
        encoding="utf-8",
    )
    return proj


def _only_aws_profile_env(monkeypatch) -> None:
    """The reproduction shell shape: AWS_PROFILE set, NO region source anywhere in env."""
    for var in ("REBAR_LLM_BEDROCK_REGION", "AWS_DEFAULT_REGION", "AWS_REGION"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.delenv("REBAR_LLM_CONFIG_FILE", raising=False)
    monkeypatch.setenv("AWS_PROFILE", "frontier")  # a profile carrying no region key


def test_table_region_alone_builds_the_provider_in_a_profile_only_shell(
    monkeypatch, tmp_path
) -> None:
    """adb6 AC2 shape. Only AWS_PROFILE-like env — no REBAR_LLM_BEDROCK_REGION,
    AWS_DEFAULT_REGION, or AWS_REGION — and the rebar.toml table value must resolve the
    region and reach the boto3 session explicitly, so gated ops stop failing closed."""
    from rebar import config as _root_config
    from rebar.llm.bedrock_model import build_bedrock_provider
    from rebar.llm.config import LLMConfig

    provider_seen = _stub_bedrock_provider(monkeypatch)
    _only_aws_profile_env(monkeypatch)
    session_seen = _spy_boto_session(monkeypatch)
    _root_config.reset_config_cache()
    try:
        cfg = LLMConfig.from_env(repo_root=_region_pinned_project(tmp_path))
    finally:
        _root_config.reset_config_cache()

    assert cfg.bedrock_region_name == "us-east-1"
    build_bedrock_provider(cfg)
    assert session_seen["session_kwargs"].get("region_name") == "us-east-1"
    assert provider_seen.get("bedrock_client") is session_seen["sentinel"]


def test_env_region_keeps_precedence_over_the_table_value(monkeypatch, tmp_path) -> None:
    """adb6 AC3. REBAR_LLM_BEDROCK_REGION still overrides the rebar.toml pin, so an
    operator pinning a different region regresses nothing."""
    from rebar import config as _root_config
    from rebar.llm.config import LLMConfig

    _only_aws_profile_env(monkeypatch)
    monkeypatch.setenv("REBAR_LLM_BEDROCK_REGION", "eu-central-1")
    _root_config.reset_config_cache()
    try:
        cfg = LLMConfig.from_env(repo_root=_region_pinned_project(tmp_path))
    finally:
        _root_config.reset_config_cache()

    assert cfg.bedrock_region_name == "eu-central-1"


def test_this_checkout_pins_the_region_beside_its_region_scoped_model_pins() -> None:
    """adb6 AC1 (the fix itself, held as a guard). This repo's rebar.toml pins region-scoped
    `us.*` Bedrock inference profiles; the region they are measured in (us-east-1, account
    896586841071 — .github/llm-providers/bedrock.toml, external-integration.yml defaults)
    must be pinned beside them, or fresh shells rediscover the region via ambient env and
    fail closed (operator ruling on adb6-4762-90f6-4e30: use the project's measured region)."""
    from pathlib import Path

    import tomllib

    root = Path(__file__).resolve().parents[2]
    table = tomllib.loads((root / "rebar.toml").read_text(encoding="utf-8"))["llm"]
    assert str(table.get("model", "")).startswith("bedrock:us."), (
        "precondition drifted: rebar.toml no longer pins a region-scoped bedrock model"
    )
    assert table.get("bedrock_region_name") == "us-east-1", (
        "rebar.toml pins region-scoped us.* inference profiles but not the region they are "
        "served from — fresh AWS_PROFILE-only shells fail closed on every gated LLM op"
    )
