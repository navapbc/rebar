"""Provider-aware live-LLM readiness probe for the external tier (story f124).

WHY THIS EXISTS. Every live-LLM module in this tier used to gate on the literal
``ANTHROPIC_API_KEY``. That is correct only while the suite runs on exactly one provider, and it
fails in the WORST possible way once it does not: on a Bedrock arm — which authenticates from the
ambient AWS chain and deliberately carries no Anthropic key — every one of those tests would
``skip``, the arm would report **green**, and it would have validated nothing. A credential check
that names one provider cannot gate a multi-provider matrix.

So the probe asks two questions in order:

1. **Which provider will this run actually call?** Read it from the RESOLVED ``standard`` model
   class, i.e. from the same config layering (``REBAR_LLM_CONFIG_FILE`` over the discovered
   config) that the run itself will use — never from "which key happens to be set", which is the
   ambient-default behaviour this story removes.
2. **Is THAT provider's credential present?** Anthropic/OpenAI carry an API key; Bedrock has none
   — rebar manages no Bedrock key and authenticates from the ambient AWS chain
   (``rebar.llm.bedrock_model``), so the Bedrock answer is "boto3 resolves credentials", not "an
   env var is set".

WHAT IT DELIBERATELY DOES **NOT** CHECK: the AWS **region**. A missing region must FAIL, loudly,
with the typed ``LLMConfigError`` that names ``REBAR_LLM_BEDROCK_REGION`` (ticket a574) — folding
it into a skip predicate would convert exactly that hard error back into a green no-op, which is
the failure mode this module exists to prevent. Credential discovery and region discovery are
independent (see ``infra/runbooks/bedrock-access.md``).

Modules using this probe also expose a module-level ``_live_llm_ready`` sentinel, which
``conftest.py`` auto-marks as ``llm_live`` — the marker the provider matrix selects on and the
all-skip canary counts. ``tests/unit/test_ci_provider_matrix.py`` fails if a module imports this
one without defining the sentinel.
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


def configured_provider(repo_root: str | None = None) -> str:
    """The provider this run will actually call, read from the resolved ``standard`` class.

    Resolution goes through :func:`rebar.llm.model_classes.resolve_model_string`, so a
    ``REBAR_LLM_CONFIG_FILE`` overlay (how the CI matrix selects its arm) and the discovered
    config are honoured by the same code the run itself uses. A resolved string with no
    ``provider:`` prefix means the shipped default, :data:`DEFAULT_PROVIDER`.
    """
    try:
        from rebar.llm.model_classes import resolve_model_string
    except ImportError:  # the [agents] extra is absent (lean lane) — nothing will call anything
        return DEFAULT_PROVIDER
    resolved = resolve_model_string("standard", repo_root)
    provider, _, _ = resolved.partition(":")
    return provider or DEFAULT_PROVIDER


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


def live_llm_ready() -> bool:
    """True when a real LLM call can be made on the CONFIGURED provider."""
    return agents_extra_installed() and credential_present(configured_provider())


def _skip_reason() -> str:
    provider = configured_provider()
    if not agents_extra_installed():
        return "no live LLM: the [agents] extra is not installed"
    return (
        f"no live LLM: configured provider is {provider!r} but its credential is absent "
        f"— needs {credential_hint(provider)}"
    )


#: The shared gate for every live-LLM module in this tier. Import it and apply it; do not
#: hand-roll an ``ANTHROPIC_API_KEY`` skipif, which silently green-lights a non-Anthropic arm.
skip_without_live_llm = pytest.mark.skipif(not live_llm_ready(), reason=_skip_reason())
