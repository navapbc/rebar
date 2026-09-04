"""Capability decisions read ``ModelProfile``, never a provider-name string (story S2).

Two decisions used to string-match a provider name: ``structured.output_mode()`` consulted a
hardcoded native-output provider frozenset, and the runner's cache gate matched a provider-name
prefix on the resolved model string. Both are wrong for Bedrock-hosted Claude, whose model string
says ``bedrock`` — so caching silently switched off and structured output silently took the
prompted path.

Unit tier for the mapping (a pure function over a profile) plus the two consumers' contracts,
since the defect class is *a consumer reading the wrong signal*.

Every profile value asserted here was verified against the pinned pydantic-ai 1.107.1 rather than
assumed; the surprising ones are called out inline, because they are why the override layer exists.

**No ``boto3`` anywhere on the always-run path.** ``BedrockModelProfile`` is exported only from
``pydantic_ai.models.bedrock``, which imports ``botocore`` at module top, and ``boto3`` lives only
in the ``reviewbot`` extra — so the CI env running ``make test`` does not have it. The Bedrock
capability assertions therefore run against a field-stub, and a separate
``importorskip("boto3")`` test pins that stub against the REAL profile so it cannot drift into a
fake that passes while Bedrock breaks.
"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import pytest

pytest.importorskip("pydantic_ai")

from pydantic_ai import NativeOutput, PromptedOutput
from pydantic_ai.profiles.anthropic import anthropic_model_profile

from rebar.llm import structured

pytestmark = pytest.mark.unit


class _Verdict:
    """A stand-in output model; ``output_mode`` only needs a class to wrap."""


def _caps(model_or_str):
    from rebar.llm.capabilities import capabilities_for

    return capabilities_for(model_or_str)


# A profile stub carrying the Bedrock capability FIELDS. The values are not invented: they are what
# the real
# `BedrockProvider.model_profile("us.anthropic.claude-haiku-4-5-...")` reports, pinned by
# `test_real_bedrock_profile_exposes_the_fields_the_stub_mimics` below.
def _bedrock_claude_profile_stub():
    return SimpleNamespace(
        supports_json_schema_output=True,
        supports_thinking=True,
        bedrock_supports_prompt_caching=True,
        bedrock_supports_tool_caching=True,
        bedrock_thinking_variant="anthropic",
    )


def _bedrock_meta_profile_stub():
    """Bedrock-hosted Meta: same host, NOT Claude — the contrast case that proves the Claude
    override
    keys on
    the model family and not merely on "is this Bedrock"."""
    return SimpleNamespace(
        supports_json_schema_output=False,
        supports_thinking=False,
        bedrock_supports_prompt_caching=True,
        bedrock_supports_tool_caching=True,
        bedrock_thinking_variant=None,
    )


# ── §A happy path ───────────────────────────────────────────────────────────────────────


def test_anthropic_yields_anthropic_cache_style_and_prompted_output():
    """Direct Claude: cache style ``anthropic``, and structured output stays PROMPTED.

    The prompted result is the whole reason the override layer exists —
    ``anthropic_model_profile(...).supports_json_schema_output`` is **True** upstream, so a naive
    profile read
    would flip rebar's primary provider to NativeOutput and break the deliberate choice at
    structured.py."""
    assert anthropic_model_profile("claude-opus-4-8").supports_json_schema_output is True, (
        "premise check: upstream really does claim json_schema support for Claude"
    )

    caps = _caps("anthropic:claude-opus-4-8")
    assert caps.prompt_cache_style == "anthropic"
    assert caps.native_structured_output is False
    assert caps.supports_thinking is True
    assert isinstance(structured.output_mode(_Verdict, caps), PromptedOutput)


def test_openai_lazy_model_string_keeps_native_output():
    """A provider rebar does NOT build arrives as a lazy model STRING (story S1), and must still
    resolve a real
    profile — the conservative fallback must NOT be reached merely because construction was
    deferred.

    This works because ``pydantic_ai.profiles.*`` are pure capability records that import without
    the vendor
    SDK, unlike ``pydantic_ai.providers.*``. The `openai` package is not installed here."""
    caps = _caps("openai:gpt-4o")
    assert caps.native_structured_output is True
    assert caps.prompt_cache_style == "none"
    assert caps.supports_thinking is False
    assert isinstance(structured.output_mode(_Verdict, caps), NativeOutput)


def test_cache_settings_selected_from_style_and_absent_when_none():
    """The cache-settings seam: one function, dispatching on ``prompt_cache_style``.

    ``execution_mode="single_turn"`` is this test's original subject preserved verbatim: it is
    the arm bug dd27 left byte-unchanged, so these assertions still mean what they meant."""
    from rebar.llm.capabilities import cache_settings_for

    settings = cache_settings_for(_caps("anthropic:claude-opus-4-8"), execution_mode="single_turn")
    assert settings is not None
    assert settings["anthropic_cache_instructions"] is True
    assert settings["anthropic_cache_tool_definitions"] is True

    # A non-caching provider sends no cache keys at all (they error on openai/gemini).
    assert cache_settings_for(_caps("openai:gpt-4o"), execution_mode="single_turn") is None


# ── §B held out from the implementer ────────────────────────────────────────────────────


def test_bedrock_hosted_claude_gets_bedrock_cache_style_and_stays_prompted():
    """THE motivating defect. Bedrock-hosted Claude must get cache style ``bedrock`` (caching ON)
    and
    must
    still be PROMPTED, because it is Claude on a different host.

    Detection cannot use ``isinstance(profile, BedrockModelProfile)`` — that class needs
    botocore, absent in CI — so it keys on capability FIELDS. And it cannot key on
    ``AnthropicModelProfile`` either: Bedrock Claude's profile is a sibling class, not an
    Anthropic one."""
    caps = _caps(SimpleNamespace(profile=_bedrock_claude_profile_stub()))
    assert caps.prompt_cache_style == "bedrock"
    assert caps.native_structured_output is False, (
        "Bedrock-hosted Claude is still Claude — it must not flip to NativeOutput"
    )
    assert caps.supports_thinking is True
    assert isinstance(structured.output_mode(_Verdict, caps), PromptedOutput)


def test_cache_settings_for_bedrock_style_sets_both_bedrock_breakpoints():
    """The other half of the motivating defect: resolving ``style == "bedrock"`` is only useful if
    it actually PRODUCES the Bedrock cache settings. Bedrock-hosted Claude losing prompt caching
    is the exact regression this epic exists to prevent, so the settings object — not just the
    style string — needs an executable proof.

    ``boto3``-gated because ``BedrockModelSettings`` lives in ``pydantic_ai.models.bedrock``,
    which imports botocore; CI skips it, but it runs locally and in the Bedrock story's env."""
    pytest.importorskip("boto3")
    from rebar.llm.capabilities import cache_settings_for

    caps = _caps(SimpleNamespace(profile=_bedrock_claude_profile_stub()))
    assert caps.prompt_cache_style == "bedrock"

    # single_turn: the arm bug dd27 left byte-unchanged, so this keeps pinning what it pinned.
    settings = cache_settings_for(caps, execution_mode="single_turn")
    assert settings is not None, "Bedrock caching must be ON — this is the defect being fixed"
    assert settings["bedrock_cache_instructions"] is True
    assert settings["bedrock_cache_tool_definitions"] is True
    # The anthropic-shaped keys would ERROR on the Bedrock wire, so they must not be sent.
    assert "anthropic_cache_instructions" not in settings
    assert "anthropic_cache_tool_definitions" not in settings


def test_bedrock_hosted_non_claude_is_not_caught_by_the_claude_override():
    """Contrast case: same host, different family. Proves the override keys on the model family
    rather than on "is this Bedrock", so it cannot over-apply."""
    caps = _caps(SimpleNamespace(profile=_bedrock_meta_profile_stub()))
    assert caps.prompt_cache_style == "bedrock"  # still a Bedrock cache surface
    # ...but native_structured_output comes from ITS profile, not from the Claude override.
    assert caps.native_structured_output is False  # this profile genuinely lacks json_schema
    assert caps.supports_thinking is False


def test_real_bedrock_profile_exposes_the_fields_the_stub_mimics():
    """The verified-fake guard: pins the field-stub above against the REAL upstream profile.

    Without this, the stub is an assertion about our own invention — it could pass while real
    Bedrock breaks.
    Skipped when ``boto3`` is absent (CI), but runs locally and in S3's env."""
    pytest.importorskip("boto3")
    from pydantic_ai.providers.bedrock import BedrockProvider

    real = BedrockProvider.model_profile("us.anthropic.claude-haiku-4-5-20251001-v1:0")
    stub = _bedrock_claude_profile_stub()
    for field in (
        "supports_json_schema_output",
        "supports_thinking",
        "bedrock_supports_prompt_caching",
        "bedrock_supports_tool_caching",
        "bedrock_thinking_variant",
    ):
        assert getattr(real, field) == getattr(stub, field), (
            f"stub drifted from the real Bedrock profile on {field!r}"
        )


def test_unknown_profile_degrades_conservatively_with_exactly_one_warning(caplog):
    """Fail-safe, not fail-closed: an unrecognized model must never raise. Both fallbacks are the
    safe
    direction — PromptedOutput works everywhere and sending no cache keys is always valid.

    Exactly ONE warning: a per-call log would spam every gate invocation."""
    with caplog.at_level(logging.WARNING):
        caps = _caps(object())

    assert caps.native_structured_output is False
    assert caps.prompt_cache_style == "none"
    assert caps.supports_thinking is False

    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1, f"expected exactly one WARNING, got {len(warnings)}"
    assert isinstance(structured.output_mode(_Verdict, caps), PromptedOutput)


def test_output_mode_does_not_consult_supports_thinking():
    """``supports_thinking`` is PROVENANCE (it is stamped into S5's signed verdict), never control
    flow.

    Gemini is the discriminating case: ``supports_thinking=True`` AND json_schema support, and no
    Claude
    override — so it must still get NativeOutput. If ``output_mode`` ever read the capability flag
    instead of
    the caller's per-call ``thinking`` argument, this goes red. The Anthropic 400 is a property of
    the CALL."""
    caps = _caps("google-gla:gemini-2.5-flash")
    assert caps.supports_thinking is True
    assert caps.native_structured_output is True
    assert isinstance(structured.output_mode(_Verdict, caps), NativeOutput)


def test_thinking_argument_still_forces_prompted_output():
    """The existing per-call contract, which must not regress: extended thinking plus a
    native/forced
    output
    constraint is a documented Anthropic 400, so the caller's flag wins regardless of capability."""
    caps = _caps("openai:gpt-4o")
    assert caps.native_structured_output is True
    assert isinstance(structured.output_mode(_Verdict, caps, thinking=True), PromptedOutput)


@pytest.mark.parametrize(
    ("model_str", "expected_native"),
    [
        ("openai:gpt-4o", True),
        ("openai-chat:gpt-4o", True),
        ("openai-responses:gpt-4o", True),  # ticket 155c: capability parity with Chat
        ("google-gla:gemini-2.5-flash", True),
        ("google-vertex:gemini-2.5-flash", True),
        ("vertexai:gemini-2.5-flash", True),
        ("google:gemini-2.5-flash", True),
        ("google-cloud:gemini-2.5-flash", True),
        # DISCLOSED behavior change: groq's profile reports supports_json_schema_output=False, so it
        # moves
        # from NativeOutput (today's hardcoded set) to PromptedOutput. Deliberate, and pinned here
        # so it is
        # never mistaken for an accidental regression.
        ("groq:llama-3.3-70b-versatile", False),
    ],
)
def test_every_resolver_key_resolves_a_real_profile(model_str, expected_native):
    """Every key in the resolver registry must resolve off the lazy-string path.

    A single-provider test would pass while ``google-vertex`` silently fell through to the
    conservative
    fallback — a behavior change nobody disclosed. Parameterizing is the point."""
    caps = _caps(model_str)
    assert caps.native_structured_output is expected_native, (
        f"{model_str} resolved to native={caps.native_structured_output}; "
        "a missing resolver key silently degrades to the conservative record"
    )


def test_no_native_output_provider_is_dropped_except_the_disclosed_groq():
    """Guards the migration itself: every provider that gets NativeOutput today must still get it,
    with
    groq
    the single disclosed exception. This is the regression net for the whole frozenset removal."""
    previously_native = {
        "openai:gpt-4o": True,
        "google-gla:gemini-2.5-flash": True,
        "google-vertex:gemini-2.5-flash": True,
        "groq:llama-3.3-70b-versatile": False,  # the one disclosed change
    }
    actual = {m: _caps(m).native_structured_output for m in previously_native}
    assert actual == previously_native


# ── §S3/18ae — measured Bedrock native-output rows + native_output_with_thinking plumbing ───
#
# APPENDED to tests/unit/test_llm_capabilities.py. Uses the module's existing _Verdict, _caps,
# and _bedrock_claude_profile_stub helpers.

_MEASURED_SONNET_ID = "us.anthropic.claude-sonnet-4-6"
_MEASURED_HAIKU_ID = "us.anthropic.claude-haiku-4-5-20251001-v1:0"


def _bedrock_claude_model(model_id: str):
    """A model OBJECT carrying a Bedrock-hosted Claude profile plus the exact ``model_name`` the
    exact-id override table (story S3/18ae) matches against."""
    from types import SimpleNamespace

    return SimpleNamespace(model_name=model_id, profile=_bedrock_claude_profile_stub())


def test_measured_bedrock_sonnet_flips_to_native_under_thinking():
    """The motivating enablement: the E1-measured `us.anthropic.claude-sonnet-4-6` cell must win
    the exact-id override over the Claude family default (`native_structured_output: False`) and
    route NativeOutput — INCLUDING under extended thinking, because its row also sets
    `native_output_with_thinking: True` (E1: outputConfig json_schema + thinking wire-legal)."""
    caps = _caps(_bedrock_claude_model(_MEASURED_SONNET_ID))
    assert caps.native_structured_output is True, (
        "the exact-id override must beat the Claude family default of False"
    )
    assert caps.native_output_with_thinking is True
    assert isinstance(structured.output_mode(_Verdict, caps), NativeOutput)
    assert isinstance(structured.output_mode(_Verdict, caps, thinking=True), NativeOutput)


def test_measured_bedrock_dated_haiku_flips_to_native_under_thinking():
    """The dated haiku PROFILE id (the bare alias 400s at request validation; measured E1) is the
    second enabled cell — same contract as sonnet."""
    caps = _caps(_bedrock_claude_model(_MEASURED_HAIKU_ID))
    assert caps.native_structured_output is True
    assert caps.native_output_with_thinking is True
    assert isinstance(structured.output_mode(_Verdict, caps, thinking=True), NativeOutput)


# ── plumbing pins: native_output_with_thinking must reach the constructed record on every path ─


def test_native_under_thinking_reaches_record_on_profile_construction_site():
    """Plumbing regression pin (review BLOCK 1): `_capabilities_from_profile` must SEED the field
    in its caps dict AND pass it to the `ModelCapabilities(...)` constructor. A build that omits
    it leaves the field at its dataclass default False and the row is silently inert — this pin
    goes RED in that case."""
    caps = _caps(_bedrock_claude_model(_MEASURED_SONNET_ID))
    assert caps.native_output_with_thinking is True


def test_native_under_thinking_reaches_record_on_string_fallback_site():
    """Plumbing regression pin: the string-fallback path (a `bedrock:<id>` STRING has no profile
    resolver by design) must seed + pass the field too. Same measured cell, addressed as a
    string, must still enable native-under-thinking — otherwise the two construction paths are
    split-brain."""
    caps = _caps(f"bedrock:{_MEASURED_SONNET_ID}")
    assert caps.native_structured_output is True
    assert caps.native_output_with_thinking is True
    assert isinstance(structured.output_mode(_Verdict, caps, thinking=True), NativeOutput)


def test_fallback_chain_all_supporting_preserves_native_under_thinking():
    """Third construction site (review BLOCK/T3): `_intersect_capabilities` builds the conservative
    record for a FallbackModel chain and feeds it straight into `output_mode`. It must intersect
    `native_output_with_thinking` with `all(...)` like every sibling field — a chain whose
    candidates ALL support native-under-thinking keeps it True and routes NativeOutput under
    thinking. A build omitting the field collapses it to False and this goes RED."""
    from rebar.llm.runner_support import _intersect_capabilities

    measured = _caps(_bedrock_claude_model(_MEASURED_SONNET_ID))
    chain = _intersect_capabilities([measured, measured])
    assert chain.native_output_with_thinking is True
    assert chain.native_structured_output is True
    assert isinstance(structured.output_mode(_Verdict, chain, thinking=True), NativeOutput)


def test_fallback_chain_mixed_collapses_to_prompted_under_thinking():
    """The conservative half of the intersection: a chain mixing a measured cell with a
    non-measured Claude cell must NOT claim native-under-thinking (any candidate could answer), so
    the field collapses to False and thinking routes PromptedOutput."""
    from rebar.llm.runner_support import _intersect_capabilities

    measured = _caps(_bedrock_claude_model(_MEASURED_SONNET_ID))
    unmeasured = _caps(_bedrock_claude_model("us.anthropic.claude-opus-4-8"))
    chain = _intersect_capabilities([measured, unmeasured])
    assert chain.native_output_with_thinking is False
    assert isinstance(structured.output_mode(_Verdict, chain, thinking=True), PromptedOutput)


# ── negative controls: fail-closed cells must stay PromptedOutput ────────────────────────────


def test_opus_with_only_temperature_row_stays_prompted_under_thinking():
    """Fail-closed negative control: `us.anthropic.claude-opus-4-8` carries ONLY a
    `supports_temperature: False` row (ticket 2932/1903) and NO native-output/thinking row, so it
    keeps the Claude family default and stays PromptedOutput even under thinking. This proves the
    enablement is scoped to the exact measured ids, not the whole Bedrock-Claude family."""
    caps = _caps(_bedrock_claude_model("us.anthropic.claude-opus-4-8"))
    assert caps.native_structured_output is False
    assert caps.native_output_with_thinking is False
    assert caps.supports_temperature is False  # the row it DOES carry is untouched
    assert isinstance(structured.output_mode(_Verdict, caps, thinking=True), PromptedOutput)


def test_unknown_bedrock_claude_id_stays_prompted_under_thinking():
    """An arbitrary unmeasured Bedrock-Claude id (no override row at all) preserves the
    family-level fail-closed default — no speculative enablement."""
    caps = _caps(_bedrock_claude_model("us.anthropic.claude-sonnet-9-9"))
    assert caps.native_structured_output is False
    assert caps.native_output_with_thinking is False
    assert isinstance(structured.output_mode(_Verdict, caps, thinking=True), PromptedOutput)
