"""Model-class slots: ``trivial`` / ``standard`` / ``frontier`` (task f844).

rebar's per-pass model choice used to be inferred from a single ``cfg.model`` scalar,
which is why a non-default model silently disabled the verifier downgrade. This module
is the replacement SCHEMA and RESOLVER: three named class slots, each a provider
target, with an optional ordered fallback chain. The chain is parsed and carried here
but not yet consumed by the runner — that wiring is task cc33.

Configuring nothing must be a no-op: the built-in defaults reproduce today's model
choices exactly (``DEFAULT_MODEL`` / ``VERIFIER_DEFAULT_MODEL`` from :mod:`rebar.llm.config`,
plus the plan-review ladder's trivial rung), each resolved through :func:`infer_provider`
so no default hard-codes a provider.

Precedence per field: ``REBAR_LLM_<CLASS>_<FIELD>`` env var > the parsed config table >
the built-in default (model only). This is a one-way dependency on
:mod:`rebar.llm.config` (imports ``infer_provider``, ``DEFAULT_MODEL``,
``VERIFIER_DEFAULT_MODEL``) — ``config.py`` must never import this module back.
"""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from rebar.llm.config import DEFAULT_MODEL, VERIFIER_DEFAULT_MODEL, infer_provider
from rebar.llm.errors import LLMConfigError

# The plan-review model_ladder's cheapest rung — today's implicit "trivial" choice.
# Kept here (not in config.py, which has no headroom left) alongside its siblings.
TRIVIAL_DEFAULT_MODEL = "claude-haiku-4-5"

CLASS_NAMES: tuple[str, ...] = ("frontier", "standard", "trivial")

_DEFAULT_MODEL_BY_CLASS: dict[str, str] = {
    "frontier": DEFAULT_MODEL,
    "standard": VERIFIER_DEFAULT_MODEL,
    "trivial": TRIVIAL_DEFAULT_MODEL,
}


@dataclass(frozen=True)
class FallbackTarget:
    """One entry in a class slot's ``fallback`` chain: a provider target with the same
    shape as a slot minus ``fallback`` itself (non-recursive — see :func:`parse_class_slots`)."""

    model: str
    provider: str | None = None
    endpoint: str | None = None


@dataclass(frozen=True)
class ClassSlot:
    """A resolved model-class slot: the primary target plus its ordered fallback chain."""

    model: str
    provider: str | None = None
    endpoint: str | None = None
    fallback: tuple[FallbackTarget, ...] = field(default_factory=tuple)


def _reject_api_key(raw: Mapping[str, Any], *, what: str) -> None:
    # Never echo the offending value in the message -- it may be a credential.
    if "api_key" in raw:
        raise LLMConfigError(f"api_key is not permitted on {what}; set credentials via env vars")


def _parse_fallback_entry(raw: Mapping[str, Any]) -> FallbackTarget:
    _reject_api_key(raw, what="a fallback entry")
    if "fallback" in raw:
        raise LLMConfigError(
            "a fallback entry must not itself carry 'fallback' -- fallback chains are "
            "non-recursive (one level only)"
        )
    model = raw.get("model")
    if not model:
        raise LLMConfigError("a fallback entry requires a non-empty 'model'")
    return FallbackTarget(model=model, provider=raw.get("provider"), endpoint=raw.get("endpoint"))


# (class, field) -> the literal env-var name. Spelled out one string per entry -- NOT
# built with an f-string -- because `scripts/gen_env_registry.py` (the generator behind
# the CI doc-drift gate for `docs/env-vars.md`) statically scans for a string-LITERAL
# argument to `os.environ.get(...)`/`os.getenv(...)`; a dynamically formatted name
# resolves correctly at runtime but is invisible to that scanner, so it would silently
# escape the documented env-var registry. Do not "tidy" this back into an f-string.
_ENV_VAR_NAMES: dict[tuple[str, str], str] = {
    ("frontier", "model"): "REBAR_LLM_FRONTIER_MODEL",
    ("frontier", "provider"): "REBAR_LLM_FRONTIER_PROVIDER",
    ("frontier", "endpoint"): "REBAR_LLM_FRONTIER_ENDPOINT",
    ("standard", "model"): "REBAR_LLM_STANDARD_MODEL",
    ("standard", "provider"): "REBAR_LLM_STANDARD_PROVIDER",
    ("standard", "endpoint"): "REBAR_LLM_STANDARD_ENDPOINT",
    ("trivial", "model"): "REBAR_LLM_TRIVIAL_MODEL",
    ("trivial", "provider"): "REBAR_LLM_TRIVIAL_PROVIDER",
    ("trivial", "endpoint"): "REBAR_LLM_TRIVIAL_ENDPOINT",
}


def _env_override(class_name: str, field_name: str) -> str | None:
    # `_ENV_VAR_NAMES` only supplies the literal name to LOOK UP; the read itself must
    # still be a literal `os.environ.get("REBAR_LLM_...")` call for the scanner above to
    # see it, so every one of the nine names is read through its own literal call here
    # rather than a single `os.environ.get(_ENV_VAR_NAMES[...])` indirection.
    literal_reads: dict[str, str | None] = {
        "REBAR_LLM_FRONTIER_MODEL": os.environ.get("REBAR_LLM_FRONTIER_MODEL"),
        "REBAR_LLM_FRONTIER_PROVIDER": os.environ.get("REBAR_LLM_FRONTIER_PROVIDER"),
        "REBAR_LLM_FRONTIER_ENDPOINT": os.environ.get("REBAR_LLM_FRONTIER_ENDPOINT"),
        "REBAR_LLM_STANDARD_MODEL": os.environ.get("REBAR_LLM_STANDARD_MODEL"),
        "REBAR_LLM_STANDARD_PROVIDER": os.environ.get("REBAR_LLM_STANDARD_PROVIDER"),
        "REBAR_LLM_STANDARD_ENDPOINT": os.environ.get("REBAR_LLM_STANDARD_ENDPOINT"),
        "REBAR_LLM_TRIVIAL_MODEL": os.environ.get("REBAR_LLM_TRIVIAL_MODEL"),
        "REBAR_LLM_TRIVIAL_PROVIDER": os.environ.get("REBAR_LLM_TRIVIAL_PROVIDER"),
        "REBAR_LLM_TRIVIAL_ENDPOINT": os.environ.get("REBAR_LLM_TRIVIAL_ENDPOINT"),
    }
    value = literal_reads[_ENV_VAR_NAMES[(class_name, field_name)]]
    return value if value else None


def _parse_slot(name: str, raw: Mapping[str, Any]) -> ClassSlot:
    _reject_api_key(raw, what=f"the '{name}' class slot")

    model = raw.get("model") or _DEFAULT_MODEL_BY_CLASS[name]
    provider = raw.get("provider")
    endpoint = raw.get("endpoint")

    # Env layer wins per-field, over both the table and the built-in default, while
    # leaving the slot's other fields untouched.
    model = _env_override(name, "model") or model
    provider = _env_override(name, "provider") or provider
    endpoint = _env_override(name, "endpoint") or endpoint

    fallback_raw = raw.get("fallback") or []
    fallback = tuple(_parse_fallback_entry(entry) for entry in fallback_raw)

    return ClassSlot(model=model, provider=provider, endpoint=endpoint, fallback=fallback)


def parse_class_slots(table: Mapping[str, Any] | None) -> dict[str, ClassSlot]:
    """Parse a ``{class_name: {model, provider?, endpoint?, fallback?}}`` mapping (e.g. the
    ``[tool.rebar.llm.model_classes]`` config table) into resolved :class:`ClassSlot`\\ s for
    all three classes, applying the ``REBAR_LLM_<CLASS>_<FIELD>`` env overrides and the
    built-in defaults for any class left unset. Raises :class:`LLMConfigError` for an
    unknown class name, an ``api_key`` on any slot or fallback entry, or a nested
    ``fallback`` inside a fallback entry."""
    table = table or {}
    unknown = sorted(set(table) - set(CLASS_NAMES))
    if unknown:
        raise LLMConfigError(f"unknown model-class name(s): {', '.join(unknown)}")

    return {name: _parse_slot(name, table.get(name) or {}) for name in CLASS_NAMES}


def _resolve_target(model: str, provider: str | None) -> str:
    # Already provider-qualified -- return as-is, never double-prefix.
    if ":" in model:
        return model
    resolved_provider = provider or infer_provider(model)
    return f"{resolved_provider}:{model}" if resolved_provider else model


def resolve_class(name: str, slots: Mapping[str, ClassSlot]) -> str:
    """The primary ``provider:model`` string for class ``name``. Raises
    :class:`LLMConfigError` if ``name`` is not a known model class."""
    if name not in CLASS_NAMES:
        raise LLMConfigError(f"unknown model class: {name!r} is not one of {CLASS_NAMES}")
    slot = slots[name]
    return _resolve_target(slot.model, slot.provider)


def resolve_fallback_chain(name: str, slots: Mapping[str, ClassSlot]) -> list[str]:
    """The ordered ``provider:model`` fallback chain for class ``name`` (empty if none
    configured). Order is load-bearing: fallbacks are tried in declaration order. Raises
    :class:`LLMConfigError` if ``name`` is not a known model class."""
    if name not in CLASS_NAMES:
        raise LLMConfigError(f"unknown model class: {name!r} is not one of {CLASS_NAMES}")
    return [_resolve_target(t.model, t.provider) for t in slots[name].fallback]
