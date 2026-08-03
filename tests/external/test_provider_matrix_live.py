"""The provider matrix's own arm-integrity checks, run INSIDE each arm (story f124).

The unit tier can prove things about the workflow FILE (see
``tests/unit/test_ci_provider_matrix.py``); only a test running inside the arm can prove things
about the arm's REALISED environment. That is what these are for, and each corresponds to a way
the matrix could be silently wrong while every other test still passed:

* **the arm ran the provider it claims.** The workflow declares the arm's provider in
  ``REBAR_EXPECTED_LLM_PROVIDER`` and selects it through a ``REBAR_LLM_CONFIG_FILE`` overlay. If
  the pointer were mis-pathed, unreadable, or shadowed, every live test would still pass — on the
  DEFAULT provider — and the arm would report a green Bedrock run that never touched Bedrock.
* **the arm holds NO other provider's credential.** A Bedrock arm that also carried
  ``ANTHROPIC_API_KEY`` could fall back to direct Anthropic on any path that reads a key rather
  than the resolved model string, and the fallback would look like success.
* **Bedrock resolved a region.** MEASURED (ticket a574): no region resolves from IMDS, and
  ``build_bedrock_provider`` then raises a typed ``LLMConfigError``. Asserting a region resolved
  turns "the arm is one env var away from a hard failure" into a visible test result.

All of these are skipped when ``REBAR_EXPECTED_LLM_PROVIDER`` is unset — i.e. everywhere except
CI — because a workstation legitimately has several providers' keys exported at once and the
matrix's single-credential discipline is a property of the CI arm, not of a developer's shell.

No model call is made here, but the module carries the standard live-LLM gate so that an arm with
no credential skips these too; otherwise this module alone would keep executing and the
``llm-live-canary`` all-skip check (tests/external/conftest.py) could never fire.
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
    """`cfg.model` is a SECOND resolution path, and checking only the CLASSES misses it.

    This test exists because its sibling above did NOT catch a real leak. In the f124 incident the
    class assertion PASSED on all three arms while three tests still called
    `model=anthropic:claude-opus-4-8`, because an op that resolves `cfg.model` rather than naming a
    class never consults the class table at all: `config.py` falls back to `DEFAULT_MODEL`, the bare
    literal "claude-opus-4-8", which infers provider `anthropic`. On a non-Anthropic arm those calls
    then failed with "Could not resolve authentication method" — the arm's `ANTHROPIC_API_KEY` is
    blanked deliberately.

    So the class assertion alone is over the WRONG SURFACE for this story's stated goal of removing
    the ambient default. The overlay now sets `[llm] model` as well, and this pins it."""
    from rebar.llm.config import LLMConfig

    resolved = LLMConfig.from_env().model
    assert resolved.startswith(f"{_expected}:"), (
        f"arm declares provider {_expected!r} but the ambient cfg.model resolves to {resolved!r} — "
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

    resolved = {c: resolve_model_string(c) for c in _CLASSES}
    wrong = {c: m for c, m in resolved.items() if not m.startswith(f"{_expected}:")}
    assert not wrong, (
        f"arm declares provider {_expected!r} but these model classes resolve elsewhere: "
        f"{wrong} — check REBAR_LLM_CONFIG_FILE "
        f"({os.environ.get('REBAR_LLM_CONFIG_FILE')!r}) is readable and sets "
        f"[llm.model_classes] for every class"
    )
    assert _live_llm.configured_provider() == _expected


@_live_llm.skip_without_live_llm
@_skip_unless_ci_arm
def test_the_arm_carries_no_other_providers_credential() -> None:
    """Only the declared provider's credential may be present.

    This is the runtime half of "the Bedrock arm does not silently fall back to
    ANTHROPIC_API_KEY": the workflow guards every key expression on ``matrix.provider``, and this
    asserts the realised environment matches. An empty-string value counts as absent (that is
    what a guarded GitHub Actions expression evaluates to on a non-matching arm).
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
    """Bedrock only: a region resolved, from BOTH the rebar knob and the AWS-standard var.

    Ticket a574: IMDS supplies NO region, so credential discovery succeeding says nothing about
    region discovery. rebar's own knob alone was ALSO insufficient there, which is why the arm
    sets both. If either is missing the provider build raises a typed ``LLMConfigError`` (pinned by
    ``test_missing_region_raises_a_typed_error_naming_the_setting`` in
    ``tests/unit/test_bedrock_provider.py``) — this test makes that latent hard failure visible as
    a named assertion instead.
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
