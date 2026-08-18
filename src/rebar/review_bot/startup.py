"""RP-04 S5 (5851) — review-bot startup binding + decision-auth validation.

A long-running review-bot process composes its NON-SECRET startup policy and its live
provider-native LLM binding ONCE, at startup, into an immutable :class:`StartupBinding`.
A running process does NOT rebind on ambient change; a restart/redeploy (a fresh
:func:`compose_startup_binding`) is what observes rotated non-secret material (AC4). The
SDK-native credential refresh the provider owns stays SDK-owned — the composed
:class:`~rebar.llm.auth.LLMRuntime` here is the all-``None`` ambient runtime, byte-identical
to the RP-01 provider path, and is forwarded provider-native into each review operation.

:func:`validate_decision_auth` is the fail-closed guard (AC3): the review-bot's
decision-bearing Gerrit auth is validated BEFORE any job/provider work; a missing, blank, or
whitespace-only token raises :class:`DecisionAuthError` so the caller casts NO vote and NEVER
falls back to an anonymous or alternate principal.

The binding's :attr:`StartupBinding.policy` is deliberately NON-SECRET — it carries only the
project/base-url/bot-user identity, never the ``gerrit_bot_token`` or ``webhook_token`` — and
is wrapped in a read-only mapping so it cannot be mutated after compose.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from rebar.llm.auth import LLMRuntime

if TYPE_CHECKING:
    from rebar.review_bot.config import ReceiverConfig


class DecisionAuthError(RuntimeError):
    """The review-bot's decision-bearing Gerrit auth is absent (missing/blank/whitespace).

    Raised by :func:`validate_decision_auth` BEFORE any provider/job work so the caller
    fails closed — casting NO vote and NEVER degrading to an anonymous or alternate
    principal."""


@dataclass(frozen=True)
class StartupBinding:
    """The immutable startup snapshot: the provider-native LLM runtime composed once, plus a
    read-only non-secret policy mapping. Frozen so a running process cannot rebind it; a fresh
    :func:`compose_startup_binding` (restart/redeploy) is the only way to pick up rotated
    non-secret material."""

    llm_runtime: LLMRuntime
    policy: Mapping[str, Any]


def compose_startup_binding(cfg: ReceiverConfig) -> StartupBinding:
    """Compose the review-bot's startup binding ONCE from ``cfg``.

    The LLM runtime is the all-``None`` ambient :class:`~rebar.llm.auth.LLMRuntime` (the
    review-bot uses ambient provider auth; the SDK owns credential refresh). The captured
    policy is NON-SECRET — the project/base-url/bot-user identity only, never the
    ``gerrit_bot_token``/``webhook_token`` — and is wrapped read-only so the returned binding
    is fully immutable. Pure w.r.t. ``cfg``: reads only ``cfg`` and never re-reads ambient env.
    """
    policy: Mapping[str, Any] = MappingProxyType(
        {
            "project": cfg.project,
            "gerrit_base_url": cfg.gerrit_base_url,
            "bot_user": cfg.bot_user,
        }
    )
    return StartupBinding(llm_runtime=LLMRuntime(), policy=policy)


def validate_decision_auth(cfg: ReceiverConfig) -> None:
    """Validate the decision-bearing Gerrit auth BEFORE any job/provider work.

    Raises :class:`DecisionAuthError` when ``cfg.gerrit_bot_token`` is missing, blank, or
    whitespace-only; returns ``None`` when a real token is present. This guard NEVER falls
    back to another principal — an absent token means NO vote."""
    if not cfg.gerrit_bot_token or not cfg.gerrit_bot_token.strip():
        raise DecisionAuthError(
            "review-bot decision-bearing Gerrit auth is absent (gerrit_bot_token is "
            "missing/blank): failing closed with no vote and no fallback principal"
        )
