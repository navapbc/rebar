"""Provider-native pre-client auth carriers — the NON-SERIALIZABLE ``LLMRuntime`` seam.

RP-04 S4. A caller who already holds a provider-native session/credential object (a
signed ``AsyncAnthropic`` OAuth token, a caller-owned ``boto3.Session``, a rotating
OpenAI key callable) hands it to rebar through :class:`LLMRuntime`; rebar still
constructs, owns, and closes the official client — the runtime only supplies the
pre-client auth material the per-provider builder unwraps at the moment of
construction.

This is a strict LEAF: pure frozen-dataclass data plus fail-closed validation
helpers. It imports nothing heavy and never constructs a provider SDK object
(``AsyncAnthropic``/``boto3``/``OpenAIProvider``), builds an ``Agent``, or calls
``_pai_structured`` — the only cross-module import is the shared error vocabulary
(``rebar.llm.errors``, itself a leaf), so ``import rebar.llm`` stays stdlib-only.

Secrets live ONLY inside these carriers. Every secret-bearing field is
``repr=False`` so a carrier's ``repr``/``str`` never leaks the key/token, and the
validation helpers below never interpolate secret material into an error message.
The carriers are deliberately absent from ``LLMConfig``/``RunRequest``/snapshots/
fingerprints/caches/CLI+MCP schemas; a builder unwraps the secret only at the
instant it constructs the selected provider's client.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from rebar.llm.errors import LLMConfigError


@dataclass(frozen=True)
class AnthropicAuth:
    """Native Anthropic pre-client credential: EXACTLY ONE of an API key or an OAuth
    ``auth_token`` (e.g. a Claude-Code session token). Both fields are ``repr=False``."""

    api_key: str | None = field(default=None, repr=False)
    auth_token: str | None = field(default=None, repr=False)


@dataclass(frozen=True)
class BedrockAuth:
    """Native Bedrock pre-client session: a caller-owned ``boto3.Session`` (or a
    compatible object exposing ``.client(...)``). ``repr=False`` — a boto3 Session
    carries resolved credentials."""

    session: Any = field(default=None, repr=False)


@dataclass(frozen=True)
class OpenAIAuth:
    """Native OpenAI(-compatible) pre-client key: a ``str`` or a zero-arg ``callable``
    returning one (a rotating-key provider). ``repr=False``."""

    api_key: str | Callable[[], str] | None = field(default=None, repr=False)


@dataclass(frozen=True)
class LLMRuntime:
    """The NON-SERIALIZABLE per-run capability threaded ``get_runner`` ->
    ``PydanticAIRunner`` -> ``ProviderSession``. Each carrier is OPTIONAL and only the
    SELECTED provider's carrier is ever consumed; ``LLMRuntime()`` (all ``None``) is
    byte-identical to the ambient RP-01 path."""

    anthropic: AnthropicAuth | None = None
    bedrock: BedrockAuth | None = None
    openai: OpenAIAuth | None = None


def anthropic_auth_kwargs(auth: AnthropicAuth) -> dict[str, Any]:
    """Fail-closed validation of a SUPPLIED :class:`AnthropicAuth`, returning the exact
    ``AsyncAnthropic`` kwargs to inject (``{"api_key": ...}`` or ``{"auth_token": ...}``).

    A carrier supplied for the provider being built must name exactly one principal:
    BOTH fields set is a conflict, NEITHER set is an empty carrier — each raises
    :class:`LLMConfigError` BEFORE any client is constructed, never degrading to the
    ambient/anonymous credential. The message never echoes the secret value."""
    has_key = bool(auth.api_key)
    has_token = bool(auth.auth_token)
    if has_key and has_token:
        raise LLMConfigError(
            "AnthropicAuth carries both api_key and auth_token; supply exactly one"
        )
    if not has_key and not has_token:
        raise LLMConfigError(
            "AnthropicAuth carries neither api_key nor auth_token; supply exactly one "
            "(an empty carrier must not silently degrade to the ambient credential)"
        )
    if has_key:
        return {"api_key": auth.api_key}
    return {"auth_token": auth.auth_token}


def bedrock_session(auth: BedrockAuth) -> Any:
    """Fail-closed validation of a SUPPLIED :class:`BedrockAuth`, returning its session.

    An empty carrier (``session`` unset) raises :class:`LLMConfigError` rather than
    silently falling back to an ambient ``boto3.session.Session``."""
    if not auth.session:
        raise LLMConfigError(
            "BedrockAuth carries no session; supply a boto3 Session "
            "(an empty carrier must not silently degrade to the ambient credential chain)"
        )
    return auth.session
