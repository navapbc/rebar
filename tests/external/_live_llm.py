"""Provider-aware readiness checks for the external LLM tier.

The probe resolves the ``standard`` model through project configuration, maps protocol
qualifiers to a provider family, and checks that provider's credential. Key-authenticated
providers use their configured environment variables. Bedrock uses the boto3 credential chain.

Region validation stays outside readiness so a missing Bedrock region raises ``LLMConfigError``
instead of turning the run into a skip. Modules expose ``_live_llm_ready`` so ``conftest.py`` can
apply ``llm_live`` and detect an arm where every provider-backed test skipped.
"""

from __future__ import annotations

import os

import pytest

#: The provider assumed when nothing in the config carries an explicit ``provider:`` prefix.
#: Mirrors rebar's own shipped default (``anthropic:``-prefixed model classes).
DEFAULT_PROVIDER = "anthropic"

#: Env var each arm's credential lives in, per provider. Bedrock is absent BY DESIGN: it has no
#: key of its own (see the module docstring), so it is probed through boto3 instead.
_PROVIDER_KEY_ENV = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
}

#: rebar's provider-agnostic key knob — accepted for ANY key-authenticated provider, so a
#: best-effort OpenAI-compatible endpoint (story S4) is not forced to borrow OPENAI_API_KEY.
_GENERIC_KEY_ENV = "REBAR_LLM_API_KEY"

#: Translate protocol qualifiers to the provider families used by workflow arms and credential
#: lookup. Rebar selects ``openai-chat`` while the matrix names the family ``openai``.
_PROVIDER_FAMILY_BY_QUALIFIER = {
    "openai-chat": "openai",
    "openai-responses": "openai",
}


def provider_family(qualifier: str) -> str:
    """The provider FAMILY a resolved model qualifier belongs to.

    Returns the family for a known protocol-specific qualifier (e.g. ``openai-chat`` →
    ``openai``); any other qualifier is already its own family and is returned unchanged.
    """
    return _PROVIDER_FAMILY_BY_QUALIFIER.get(qualifier, qualifier)


def configured_provider(repo_root: str | None = None) -> str:
    """Resolve the configured ``standard`` model and return its provider family.

    This uses the same discovery and ``REBAR_LLM_CONFIG_FILE`` layering as execution. Protocol
    qualifiers are normalized through :func:`provider_family`, and unqualified model strings
    use :data:`DEFAULT_PROVIDER`.
    """
    try:
        from rebar.llm.model_classes import resolve_model_string
    except ImportError:  # the [agents] extra is absent (lean lane) — nothing will call anything
        return DEFAULT_PROVIDER
    resolved = resolve_model_string("standard", repo_root)
    provider, _, _ = resolved.partition(":")
    if not provider:
        return DEFAULT_PROVIDER
    return provider_family(provider)


def _aws_credentials_resolvable() -> bool:
    """True when boto3's own chain finds credentials (instance role / env / profile / OIDC).

    Deliberately credentials ONLY — ``Session().get_credentials()`` is not a region check, and
    conflating the two is the trap ``infra/runbooks/bedrock-access.md`` documents.
    """
    try:
        import boto3
    except ImportError:
        return False
    try:
        return boto3.session.Session().get_credentials() is not None
    except Exception:  # noqa: BLE001 — a broken/partial AWS config reads as "no credential"
        return False


def credential_present(provider: str) -> bool:
    """True when *provider*'s own credential is available in this environment."""
    if provider == "bedrock":
        return _aws_credentials_resolvable()
    if os.environ.get(_GENERIC_KEY_ENV):
        return True
    env_name = _PROVIDER_KEY_ENV.get(provider)
    return bool(env_name and os.environ.get(env_name))


def credential_hint(provider: str) -> str:
    """Human-readable name of the credential *provider* needs (for skip/error messages)."""
    if provider == "bedrock":
        return "ambient AWS credentials (instance role / AWS_PROFILE / OIDC role assumption)"
    return _PROVIDER_KEY_ENV.get(provider, _GENERIC_KEY_ENV)


def agents_extra_installed() -> bool:
    """True when the ``[agents]`` extra is importable (no live call is possible without it)."""
    try:
        import rebar.llm as llm
    except ImportError:
        return False
    return bool(llm.agents_extra_installed())


def live_llm_ready(required_provider: str | None = None) -> bool:
    """Return whether the provider selected for this module has usable credentials.

    ``required_provider`` covers modules that pin a model instead of following the matrix arm.
    Such modules are ready only when the arm resolves that provider and its credential is present.
    """
    if not agents_extra_installed():
        return False
    provider = configured_provider()
    if required_provider is not None and provider != required_provider:
        return False
    return credential_present(required_provider or provider)


def _skip_reason(required_provider: str | None = None) -> str:
    provider = configured_provider()
    if not agents_extra_installed():
        return "no live LLM: the [agents] extra is not installed"
    if required_provider is not None and provider != required_provider:
        return (
            f"no live LLM: this module pins a {required_provider!r} model, but the configured "
            f"arm resolves {provider!r} — that arm cannot cover it, so it runs on the "
            f"{required_provider!r} arm instead"
        )
    target = required_provider or provider
    return (
        f"no live LLM: configured provider is {target!r} but its credential is absent "
        f"— needs {credential_hint(target)}"
    )


#: The shared gate for every live-LLM module in this tier. Import it and apply it; do not
#: hand-roll an ``ANTHROPIC_API_KEY`` skipif, which silently green-lights a non-Anthropic arm.
skip_without_live_llm = pytest.mark.skipif(not live_llm_ready(), reason=_skip_reason())


def skip_unless_provider(required_provider: str) -> pytest.MarkDecorator:
    """Skip a provider-pinned module when its provider does not match the matrix arm.

    The reason names both providers. Arm-following modules still execute, so this skip does not
    defeat credential preflight or the all-skip canary.
    """
    return pytest.mark.skipif(
        not live_llm_ready(required_provider),
        reason=_skip_reason(required_provider),
    )
