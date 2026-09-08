"""Runtime integrity checks for each external provider-matrix arm.

The workflow declares an expected provider family and selects it through a configuration overlay.
These checks require the resolved model classes and scalar ``cfg.model`` to match that family,
allow only its credential to be nonblank, and require both Bedrock region settings.

CI sets ``REBAR_EXPECTED_LLM_PROVIDER`` so each arm can compare its resolved configuration with
the declared family. The standard readiness sentinel keeps an arm without credentials eligible
for the all-skip canary without making a model call.
"""

from __future__ import annotations

import os

import _live_llm
import pytest

pytestmark = pytest.mark.external

# Auto-marks this module's tests `llm_live` (tests/external/conftest.py).
_live_llm_ready = _live_llm.live_llm_ready()

_CLASSES = ("trivial", "standard", "frontier")

#: Set by each matrix arm to the provider that arm is FOR. Absent off-CI.
_EXPECTED_ENV = "REBAR_EXPECTED_LLM_PROVIDER"

_expected = (os.environ.get(_EXPECTED_ENV) or "").strip()

_skip_unless_ci_arm = pytest.mark.skipif(
    not _expected,
    reason=f"{_EXPECTED_ENV} is unset — not running inside a CI provider-matrix arm",
)


@_live_llm.skip_without_live_llm
@_skip_unless_ci_arm
def test_the_ambient_default_model_also_resolves_to_the_declared_provider() -> None:
    """Require scalar ``cfg.model`` to resolve to the arm's provider family.

    Model classes are a separate resolution surface. Checking both prevents the scalar default
    from sending an arm to another provider.
    """
    from rebar.llm.config import LLMConfig

    resolved = LLMConfig.from_env().model
    qualifier, sep, _ = resolved.partition(":")
    assert sep and _live_llm.provider_family(qualifier) == _expected, (
        f"arm declares provider family {_expected!r} but the ambient cfg.model resolves to "
        f"{resolved!r}, whose qualifier's provider family does not match — "
        f"an op that reads cfg.model instead of naming a class would call the wrong provider. "
        f"Check that REBAR_LLM_CONFIG_FILE "
        f"({os.environ.get('REBAR_LLM_CONFIG_FILE')!r}) sets an [llm] model key, not only "
        f"[llm.model_classes]"
    )


def test_every_model_class_resolves_to_the_declared_provider() -> None:
    """The overlay actually took effect, for ALL THREE classes — not just the one a given op
    happens to use. A partial overlay would leave some ops on the default provider, which is the
    "ambient default" this story removes."""
    from rebar.llm.model_classes import resolve_model_string

    def _matches_family(model: str) -> bool:
        # The resolver emits protocol-specific qualifiers (e.g. openai-chat, ticket 1d22);
        # the arm declares the FAMILY. An unqualified string (no ":") never matches.
        qualifier, sep, _ = model.partition(":")
        return bool(sep) and _live_llm.provider_family(qualifier) == _expected

    resolved = {c: resolve_model_string(c) for c in _CLASSES}
    wrong = {c: m for c, m in resolved.items() if not _matches_family(m)}
    assert not wrong, (
        f"arm declares provider family {_expected!r} but these model classes resolve to a "
        f"different family: "
        f"{wrong} — check REBAR_LLM_CONFIG_FILE "
        f"({os.environ.get('REBAR_LLM_CONFIG_FILE')!r}) is readable and sets "
        f"[llm.model_classes] for every class"
    )
    assert _live_llm.configured_provider() == _expected


@_live_llm.skip_without_live_llm
@_skip_unless_ci_arm
def test_the_arm_carries_no_other_providers_credential() -> None:
    """Require only the declared provider credential to be nonblank.

    Guarded CI expressions become empty strings on other arms, which count as absent.
    """
    foreign = {
        name: provider
        for provider, name in (("anthropic", "ANTHROPIC_API_KEY"), ("openai", "OPENAI_API_KEY"))
        if provider != _expected and os.environ.get(name)
    }
    assert not foreign, (
        f"arm declares provider {_expected!r} but also carries foreign provider "
        f"credentials {sorted(foreign)} — a key-reading path could fall back to that "
        f"provider and the arm would report a green run for the wrong one"
    )


@_live_llm.skip_without_live_llm
@_skip_unless_ci_arm
def test_bedrock_arm_resolved_a_region() -> None:
    """Require both Bedrock region variables on the Bedrock arm.

    Credentials do not supply a region. Missing either setting would fail provider construction
    with ``LLMConfigError``.
    """
    if _expected != "bedrock":
        pytest.skip("region resolution is a Bedrock-arm concern")
    assert os.environ.get("REBAR_LLM_BEDROCK_REGION"), (
        "REBAR_LLM_BEDROCK_REGION is unset on the Bedrock arm — rebar's own knob is what puts "
        "the region into the verdict's provider provenance"
    )
    assert os.environ.get("AWS_DEFAULT_REGION"), (
        "AWS_DEFAULT_REGION is unset on the Bedrock arm — measured on ticket a574 as ALSO "
        "required; rebar's knob alone was insufficient"
    )
    import boto3

    assert boto3.session.Session().region_name, (
        "boto3 resolves no region despite the arm's env — Bedrock client construction would "
        "raise a typed LLMConfigError naming REBAR_LLM_BEDROCK_REGION"
    )
