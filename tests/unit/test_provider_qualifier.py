"""Provider-qualifier parsing for model ids that CONTAIN a colon (ticket 03b0).

Bedrock's canonical model ids carry a version suffix containing a colon —
``anthropic.claude-haiku-4-5-20251001-v1:0``, ``us.anthropic.claude-opus-4-5-20251101-v1:0`` —
which is the majority form AWS publishes. Two places in the stack decide "is this string already
provider-qualified?" by asking whether it contains a colon at all:

* ``model_classes._resolve_target`` — ``if ":" in model: return model`` — which SILENTLY DISCARDS a
  configured ``provider``, and
* ``config.infer_provider`` — ``model.split(":", 1)[0]`` — which then reads most of the model id
  back as though it were the provider name.

Together those produce the observed failure: a class configured
``{model: "us.anthropic.claude-haiku-4-5-20251001-v1:0", provider: "bedrock"}`` resolves to the bare
id, and the run dies with ``unknown provider 'us.anthropic.claude-haiku-4-5-20251001-v1'; registered
providers: ['anthropic', 'bedrock']`` — a message that contradicts the operator's own config, naming
a "provider" they never typed. Only the UNVERSIONED aliases (``us.anthropic.claude-sonnet-4-6``)
happen to work, which is why this stayed hidden.

The distinguishing fact is not colon POSITION but whether the prefix names a provider. A provider
name is a short identifier; a Bedrock id prefix is dotted. These tests pin that both deciders agree,
on both id shapes, and that an already-qualified string is still never double-prefixed.
"""

from __future__ import annotations

import pytest

from rebar.llm.config import _PROVIDER_PREFIXES, infer_provider, split_provider_qualifier
from rebar.llm.model_classes import _resolve_target, parse_class_slots, resolve_class

pytestmark = pytest.mark.unit

# Canonical, versioned Bedrock ids — the colon is INSIDE the version suffix.
_VERSIONED = "us.anthropic.claude-haiku-4-5-20251001-v1:0"
_VERSIONED_GLOBAL = "anthropic.claude-opus-4-5-20251101-v1:0"
# The unversioned alias form, which has no colon and worked all along.
_ALIAS = "us.anthropic.claude-sonnet-4-6"


# ── the defect: a configured provider must survive a colon-bearing model id ──────────────────


@pytest.mark.parametrize("model", [_VERSIONED, _VERSIONED_GLOBAL])
def test_configured_provider_is_applied_to_a_versioned_bedrock_id(model):
    assert _resolve_target(model, "bedrock") == f"bedrock:{model}"


def test_configured_provider_is_applied_to_an_unversioned_alias():
    """The control: this shape already worked, and must keep working byte-for-byte."""
    assert _resolve_target(_ALIAS, "bedrock") == f"bedrock:{_ALIAS}"


@pytest.mark.parametrize("model", [_VERSIONED, _VERSIONED_GLOBAL, _ALIAS])
def test_the_class_table_end_to_end_qualifies_a_bedrock_id(model):
    """Through the real config path an operator uses, not just the helper."""
    slots = parse_class_slots({"standard": {"model": model, "provider": "bedrock"}})
    assert resolve_class("standard", slots) == f"bedrock:{model}"


# ── the property the current test was protecting: never double-prefix ────────────────────────


@pytest.mark.parametrize(
    "already",
    [
        "anthropic:claude-opus-4-8",
        f"bedrock:{_VERSIONED}",
        f"bedrock:{_ALIAS}",
        "openai:gpt-4o",
        "openai-chat:gpt-4o",  # a provider name may contain a dash
        "gateway/openai:gpt-4o",  # ...or a slash
    ],
)
def test_an_already_qualified_string_is_returned_unchanged(already):
    provider = already.split(":", 1)[0]
    # Bare hosted ``openai:`` normalizes to the Responses API default (ticket 155c); every other
    # qualifier — including the explicit ``openai-chat:`` fallback — is returned unchanged.
    expected = (
        already.replace("openai:", "openai-responses:", 1) if provider == "openai" else already
    )
    assert _resolve_target(already, provider) == expected
    # …and also when NO provider is configured, so inference cannot double-prefix either.
    assert _resolve_target(already, None) == expected


# ── infer_provider must agree with the qualifier on every shape ──────────────────────────────


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("anthropic:claude-opus-4-8", "anthropic"),
        (f"bedrock:{_VERSIONED}", "bedrock"),
        ("openai:gpt-4o", "openai"),
        ("openai-chat:gpt-4o", "openai-chat"),
    ],
)
def test_infer_provider_reads_a_real_qualifier(model, expected):
    assert infer_provider(model) == expected


@pytest.mark.parametrize("model", [_VERSIONED, _VERSIONED_GLOBAL])
def test_infer_provider_does_not_mistake_a_version_suffix_for_a_qualifier(model):
    """The half of the bug that produced the misleading error message: a fragment of a model id
    must never be reported as a provider name."""
    inferred = infer_provider(model)
    assert inferred != model.split(":", 1)[0], (
        f"infer_provider returned {inferred!r} — a fragment of the model id, which is what "
        "surfaced to the operator as \"unknown provider '<most of my model id>'\""
    )
    # An unqualified Bedrock id names anthropic's family but not a provider prefix; the important
    # property is only that it is NOT the bogus fragment above.
    assert inferred in (None, "anthropic", "bedrock")


# ── back-compat: unqualified values with no configured provider are unchanged ─────────────────


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("claude-opus-4-8", "anthropic:claude-opus-4-8"),  # inferred by name prefix
        ("gpt-4o", "openai-responses:gpt-4o"),  # hosted OpenAI now defaults to the Responses API
    ],
)
def test_inference_still_qualifies_a_bare_known_model(model, expected):
    assert _resolve_target(model, None) == expected


def test_an_unknown_bare_model_is_left_alone():
    assert _resolve_target("some-private-model", None) == "some-private-model"


# ── an explicitly configured provider WINS over any inline qualifier (ticket 03b0) ────────────
#
# Guard ORDER, not just the qualifier test, is what actually fixes this bug class. LangChain's
# `init_chat_model` uses the same order — `if not model_provider and ":" in model and prefix in
# registry` — so an explicitly configured provider is never overruled by punctuation in the model
# id. Verified against langchain/chat_models/base.py:599-608.


def test_a_configured_provider_is_not_overruled_by_an_inline_qualifier():
    """The remaining half of the silent-drop bug: before this guard, a model id carrying ANY
    qualifier discarded the operator's configured provider — same defect class as the colon bug,
    just a different input."""
    with pytest.raises(Exception) as exc:
        _resolve_target("openai:gpt-4o", "bedrock")
    msg = str(exc.value)
    assert "bedrock" in msg and "openai" in msg, (
        f"the error must name BOTH the configured provider and the one in the model id: {msg}"
    )


def test_a_matching_inline_qualifier_is_not_a_conflict():
    """The control: agreement must stay silent and must not double-prefix."""
    assert _resolve_target(f"bedrock:{_VERSIONED}", "bedrock") == f"bedrock:{_VERSIONED}"
    assert _resolve_target("anthropic:claude-opus-4-8", "anthropic") == "anthropic:claude-opus-4-8"


@pytest.mark.parametrize(
    ("model", "provider", "expected"),
    [
        # Bare hosted ``openai`` (qualifier and/or provider) now defaults to the Responses API…
        ("openai:gpt-4o", "openai", "openai-responses:gpt-4o"),
        ("gpt-4o", "openai", "openai-responses:gpt-4o"),
        # …while the explicit ``openai-chat`` fallback — as a qualifier or a configured provider,
        # in any combination with the ``openai`` family — stays on Chat Completions (ticket 155c).
        ("openai-chat:gpt-4o", "openai-chat", "openai-chat:gpt-4o"),
        ("openai-chat:gpt-4o", "openai", "openai-chat:gpt-4o"),
        ("openai:gpt-4o", "openai-chat", "openai-chat:gpt-4o"),
        ("gpt-4o", "openai-chat", "openai-chat:gpt-4o"),
    ],
)
def test_openai_family_default_flips_to_responses_but_explicit_chat_stays(
    model, provider, expected
):
    assert _resolve_target(model, provider) == expected


@pytest.mark.parametrize("provider", [None, "openai"])
def test_a_custom_endpoint_keeps_the_openai_family_on_chat_completions(provider):
    """A custom OpenAI-compatible endpoint cannot use the Responses API in rebar (the builder is
    registered only under ``openai``/``openai-chat`` and vendor ``/v1/responses`` support is
    UNKNOWN), so the default flip must NOT apply when an endpoint is present (ticket 155c)."""
    endpoint = "http://local:1234/v1"
    assert _resolve_target("gpt-4o", provider, endpoint=endpoint) == "openai-chat:gpt-4o"
    assert _resolve_target("openai:gpt-4o", provider, endpoint=endpoint) == "openai-chat:gpt-4o"


def test_the_conflict_is_raised_not_silently_resolved():
    """Either choice would discard an explicit instruction, so neither is acceptable silently."""
    from rebar.llm.errors import LLMConfigError

    for model, provider in [("openai:gpt-4o", "bedrock"), ("anthropic:claude-opus-4-8", "openai")]:
        with pytest.raises(LLMConfigError):
            _resolve_target(model, provider)


# ── qualification is decided by REGISTRY MEMBERSHIP, not by prefix shape ──────────────────────
#
# The shape test this replaces asked "does this prefix LOOK like a provider name?" (an
# identifier-like `^[a-z][a-z0-9_-]*$`). The question that matters is "IS it one?". Upstream
# already answers it that way: pydantic-ai 1.107.1's `infer_model` raises `Unknown provider: X`
# for any qualifier outside its registry. Membership narrows AND widens — slashed gateway names
# were rejected by the old regex and now split correctly.

_KNOWN_25 = frozenset(
    {
        "anthropic",
        "bedrock",
        "cerebras",
        "cohere",
        "deepseek",
        "gateway/anthropic",
        "gateway/bedrock",
        "gateway/google-cloud",
        "gateway/groq",
        "gateway/openai",
        "google",
        "google-cloud",
        "google-gla",
        "google-vertex",
        "grok",
        "groq",
        "heroku",
        "huggingface",
        "mistral",
        "moonshotai",
        "openai",
        "openai-chat",
        "openai-responses",
        "vertexai",
        "xai",
    }
)


def test_known_provider_names_is_exactly_the_registry():
    """The set is a literal of plain strings so `import rebar.llm` stays stdlib-only; pinning it
    exactly is what makes the drift test below meaningful."""
    from rebar.llm.config import KNOWN_PROVIDER_NAMES

    assert KNOWN_PROVIDER_NAMES == _KNOWN_25


@pytest.mark.parametrize("name", sorted(_KNOWN_25))
def test_every_known_provider_name_splits_as_a_qualifier(name):
    """Back-compat enumerated, not assumed."""
    assert split_provider_qualifier(f"{name}:some-model") == (name, "some-model")


def test_a_slashed_provider_name_splits():
    """The case the old shape regex REJECTED: membership widens as well as narrows."""
    assert split_provider_qualifier("gateway/openai:gpt-4o") == ("gateway/openai", "gpt-4o")


# ── no name is grandfathered: `test` and `google_genai` are not providers ─────────────────────
#
# pydantic-ai 1.107.1 rejects both — `infer_model("test:foo")` and
# `infer_model("google_genai:gemini-2.0")` each raise `Unknown provider`. `test` is the special
# bare string that builds a TestModel, never a provider. Admitting either would make rebar more
# permissive than the library it wraps.


@pytest.mark.parametrize("name", ["test", "google_genai"])
def test_a_de_registered_name_is_not_a_provider(name):
    from rebar.llm.config import KNOWN_PROVIDER_NAMES

    assert name not in KNOWN_PROVIDER_NAMES


@pytest.mark.parametrize(
    "model", ["test:FunctionModel", "google_genai:gemini-2.0", "bedrok:claude-opus-4-8"]
)
def test_an_unknown_inline_prefix_is_unqualified_and_does_not_raise(model):
    """Falling back to "not qualified" — rather than raising — is load-bearing: a raise would
    break the canonical Bedrock ids 03b0 fixed, whose pre-colon prefix is not a provider name."""
    assert split_provider_qualifier(model) == (None, model)
    assert infer_provider(model) is None


@pytest.mark.parametrize("model", [_VERSIONED, _VERSIONED_GLOBAL])
def test_a_dotted_multi_colon_prefix_is_not_a_qualifier(model):
    """The property that makes the permissive inline path safe."""
    assert split_provider_qualifier(model) == (None, model)


# ── the new capability: an explicitly configured provider is validated ────────────────────────


def test_a_configured_provider_that_is_not_a_provider_name_is_rejected():
    """The motivating case: a typo is reported where the operator made it, during config
    resolution, instead of surviving into a composed target string and dying much later (or not
    at all, in a command that never reaches an LLM)."""
    from rebar.llm.errors import LLMConfigError

    with pytest.raises(LLMConfigError) as exc:
        _resolve_target("claude-opus-4-8", "bedrok")
    msg = str(exc.value)
    assert "bedrok" in msg, f"the error must name the offending value: {msg}"
    assert any(name in msg for name in _KNOWN_25), f"the error must list valid names: {msg}"


def test_a_configured_provider_that_is_a_member_still_composes():
    """The control: validation must not disturb the happy path."""
    assert _resolve_target("claude-opus-4-8", "anthropic") == "anthropic:claude-opus-4-8"
    assert split_provider_qualifier("anthropic:claude-opus-4-8") == (
        "anthropic",
        "claude-opus-4-8",
    )


def test_the_inference_path_is_not_validated():
    """The inference path composes without running the configured-provider raise: an INFERRED name
    is trusted, which is exactly why the table it comes from has to be trustworthy — see the
    whole-table invariant at the bottom of this module."""
    assert _resolve_target("gemini-2.5-pro", None) == "google:gemini-2.5-pro"


# ── the static set must not silently drift from the runtime registries ────────────────────────


def test_the_static_set_does_not_drift_from_the_runtime_registries():
    """A pydantic-ai upgrade that adds a provider fails loudly HERE, rather than silently in an
    operator's config. One-directional on purpose: a future rebar builder for a name pydantic-ai
    does not enumerate must not be a failure."""
    pytest.importorskip("pydantic_ai")

    from rebar.llm.config import KNOWN_PROVIDER_NAMES, LLMConfig
    from rebar.llm.providers import ProviderSession, _pydantic_ai_known_providers

    # `base_url` set so the conditional `openai` builder is registered too.
    session = ProviderSession(LLMConfig(base_url="https://example.invalid"))
    assert set(session._builders) <= KNOWN_PROVIDER_NAMES
    assert _pydantic_ai_known_providers() <= KNOWN_PROVIDER_NAMES


# ── every INFERRED provider name must be one a runtime registry can resolve ───────────────────
#
# `_PROVIDER_PREFIXES` is the inference table: a bare model id starting with a listed prefix is
# qualified with the mapped name, and that composed string is what the runner dispatches on.
# Nothing forced those mapped names to be names either registry knows — `gemini` mapped to
# `google_genai`, which neither pydantic-ai nor `ProviderSession` recognizes — so a bare `gemini-*`
# id composed a target that sailed through config resolution and could only fail at CALL time.
#
# The two composition sites differ, and only one broke: `anthropic_model._pai_model` passes the
# inferred name through its own private `_PAI_PROVIDER_PREFIX` table, which happened to translate
# the unusable name into a usable one; the model-class/ladder path below uses `infer_provider`'s
# result VERBATIM. So the end-to-end assertion goes through `_resolve_target`, not `_pai_model`.
#
# The invariant is pinned over the WHOLE table, not the gemini row, so a future prefix cannot
# reintroduce the class of defect.


def _resolving_session():
    from rebar.llm.config import LLMConfig
    from rebar.llm.providers import ProviderSession

    # `base_url` set so the conditional `openai` builder is registered too.
    return ProviderSession(LLMConfig(base_url="https://example.invalid"))


@pytest.mark.parametrize(
    ("prefix", "provider"), _PROVIDER_PREFIXES, ids=[p for p, _ in _PROVIDER_PREFIXES]
)
def test_every_inferred_provider_name_is_resolvable(prefix, provider):
    """`is_resolvable` is the union of both registries — a rebar builder or a name pydantic-ai's
    own `known_model_names()` enumerates — so this is one assertion covering both sides."""
    pytest.importorskip("pydantic_ai")

    assert _resolving_session().is_resolvable(provider), (
        f"prefix {prefix!r} infers provider {provider!r}, which no rebar builder and no "
        f"pydantic-ai provider name resolves; a bare {prefix}* model id cannot be constructed"
    )


def test_a_bare_gemini_id_composes_a_resolvable_target():
    """End-to-end on the path that actually broke, from the operator's bare id to the qualifier a
    provider would be built from."""
    pytest.importorskip("pydantic_ai")

    target = _resolve_target("gemini-2.5-pro", None)
    qualifier, _ = split_provider_qualifier(target)
    assert qualifier is not None, f"{target!r} is not even recognized as provider-qualified"
    assert _resolving_session().is_resolvable(qualifier), (
        f"{target!r} names provider {qualifier!r}, which cannot be built"
    )


@pytest.mark.parametrize(
    ("model", "expected"),
    [
        ("claude-opus-4-8", "anthropic"),
        ("gpt-4o", "openai"),
        ("gpt4-turbo", "openai"),
        ("o1-preview", "openai"),
        ("o3-mini", "openai"),
        ("chatgpt-4o-latest", "openai"),
        ("mystery-model", None),
    ],
)
def test_inference_for_the_other_prefixes_is_unchanged(model, expected):
    """The control: only the gemini row moves."""
    assert infer_provider(model) == expected


@pytest.mark.parametrize("model", ["google:gemini-2.5-flash", "google-gla:gemini-2.5-flash"])
def test_an_already_qualified_gemini_id_is_left_alone(model):
    """Qualified ids never reach the prefix table, so remapping it must not disturb them."""
    assert infer_provider(model) == model.split(":", 1)[0]
    assert _resolve_target(model, None) == model
