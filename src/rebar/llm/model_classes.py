"""Model-class slots: ``trivial`` / ``standard`` / ``frontier`` (task f844).

rebar's per-pass model choice used to be inferred from a single ``cfg.model`` scalar,
which is why a non-default model silently disabled the verifier downgrade. This module
is the replacement SCHEMA and RESOLVER: three named class slots, each a provider
target, with an optional ordered fallback chain. Task cc33 added the chain's runtime
half at the foot of this module — the ``FallbackModel`` construction, its ``fallback_on``
predicate, and the context manager that drives the wrapper around a synchronous run.

Configuring nothing must be a no-op: the built-in defaults reproduce today's model
choices exactly (``DEFAULT_MODEL`` / ``VERIFIER_DEFAULT_MODEL`` from :mod:`rebar.llm.config`,
plus the plan-review ladder's trivial rung), each resolved through :func:`infer_provider`
so no default hard-codes a provider.

Precedence per field: ``REBAR_LLM_<CLASS>_<FIELD>`` env var > the parsed config table >
the built-in default (model only). This is a one-way dependency on
:mod:`rebar.llm.config` (imports ``infer_provider``, ``DEFAULT_MODEL``,
``VERIFIER_DEFAULT_MODEL``) — ``config.py`` must never import this module back — plus a
call-time one on :mod:`rebar.llm.failure` for the resolution taxonomy the fallback
predicate reads.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

from rebar.llm.config import (
    DEFAULT_MODEL,
    KNOWN_PROVIDER_NAMES,
    VERIFIER_DEFAULT_MODEL,
    infer_provider,
    split_provider_qualifier,
)
from rebar.llm.errors import LLMConfigError

logger = logging.getLogger(__name__)

# The plan-review model_ladder's cheapest rung — today's implicit "trivial" choice.
# Kept here (not in config.py, which has no headroom left) alongside its siblings.
TRIVIAL_DEFAULT_MODEL = "claude-haiku-4-5"

TRIVIAL_CLASS = "trivial"
STANDARD_CLASS = "standard"
FRONTIER_CLASS = "frontier"

CLASS_NAMES: tuple[str, ...] = (FRONTIER_CLASS, STANDARD_CLASS, TRIVIAL_CLASS)

_DEFAULT_MODEL_BY_CLASS: dict[str, str] = {
    "frontier": DEFAULT_MODEL,
    "standard": VERIFIER_DEFAULT_MODEL,
    "trivial": TRIVIAL_DEFAULT_MODEL,
}

# The per-model context-window table (moved here from plan_review/sizing.py so both the
# plan-review escalation ladder and the completion verifier share one source of model
# window knowledge). Bare Anthropic family names; substring-matched by accessors.
MODEL_WINDOW_LADDER = (
    ("claude-haiku-4-5", 200_000),
    ("claude-sonnet-4-6", 1_000_000),
    ("claude-opus-4-8", 1_000_000),
)


def own_window_tokens(model: str | None) -> int:
    """The resolved model's OWN context window in tokens — NOT an escalation maximum.

    Completion does not escalate models up the ladder (plan-review does), so it needs the
    matched model's own window, not `plan_review.sizing.largest_window_tokens`'s
    at-or-above maximum. Substring rung match, exactly like the ladder lookup. A model the
    ladder cannot locate (or an absent model) → the ladder MINIMUM: under-admitting is loud
    and recoverable, over-admitting fails mid-run (the conservative default from bug 48b3).
    """
    ladder_min = min(window for _name, window in MODEL_WINDOW_LADDER)
    if model:
        for name, window in MODEL_WINDOW_LADDER:
            if name in model:
                return window
        return ladder_min
    return ladder_min


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


def _cli_override(class_name: str, field_name: str) -> str | None:
    """The ``rebar -c llm.<class>.<field>=<value>`` layer, the highest-precedence one.

    ``parse_cli_overrides`` splits a dotted key on the FIRST dot only, so
    ``-c llm.frontier.model=gpt-4o`` arrives as the sub-key ``"frontier.model"`` under section
    ``llm`` -- the dot is retained, which is why the lookup key is built the same way here.
    """
    from rebar.config import cli_overrides_for

    value = cli_overrides_for("llm").get(f"{class_name}.{field_name}")
    return value if value else None


def _parse_slot(name: str, raw: Mapping[str, Any]) -> ClassSlot:
    _reject_api_key(raw, what=f"the '{name}' class slot")

    # Precedence is CLI > per-class env > config table > built-in default. The bare
    # REBAR_LLM_MODEL used to sit at the DEFAULT position, fanning one value out to all
    # three classes; it was removed and tombstoned in the pre-1.0 breaking pass.
    model = raw.get("model") or _DEFAULT_MODEL_BY_CLASS[name]
    provider = raw.get("provider")
    endpoint = raw.get("endpoint")

    # Documented precedence, applied PER FIELD: CLI > env > config table > built-in default.
    # Per-field rather than per-object is the point -- overriding one field must never clear
    # the slot's siblings, which is the same lose-the-siblings failure the parent plan's
    # deep-merge decision guards against for the config-file layer.
    model = _cli_override(name, "model") or _env_override(name, "model") or model
    provider = _cli_override(name, "provider") or _env_override(name, "provider") or provider
    endpoint = _cli_override(name, "endpoint") or _env_override(name, "endpoint") or endpoint

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


def _openai_target(tail: str, *, endpoint: str | None) -> str:
    """Compose the hosted-OpenAI ``provider:model`` string for a bare/inferred ``openai``
    request (ticket 155c cutover).

    Responses is the default (``openai-responses:``); a custom OpenAI-compatible ``endpoint``
    forces ``openai-chat:``. Rebar's ``_build_openai`` registers only under ``openai``/
    ``openai-chat`` and an ``openai-responses:`` string never reaches it, and vendor-side
    ``/v1/responses`` support for a custom base URL is UNKNOWN under the pin — so flipping a
    custom endpoint to Responses would silently ignore its ``base_url`` (capability matrix rows
    2-3). An EXPLICIT ``openai-chat:`` qualifier / ``provider = "openai-chat"`` is honored
    separately and always stays Chat; only this bare/inferred path flips."""
    return f"openai-chat:{tail}" if endpoint else f"openai-responses:{tail}"


def _resolve_target(model: str, provider: str | None, *, endpoint: str | None = None) -> str:
    """Compose the ``provider:model`` string a runner dispatches on (ticket 03b0).

    AN EXPLICITLY CONFIGURED ``provider`` IS CHECKED FIRST, before the model string is scanned for a
    qualifier at all. That guard order is the substance of this fix, and it is the one used by
    LangChain's ``init_chat_model`` (``if not model_provider and ":" in model and prefix in
    _BUILTIN_PROVIDERS``): if the operator said which provider to use, no amount of punctuation in
    the model id can overrule or discard it. The original code scanned first and so dropped the
    configured provider for any model id containing a colon — which is every canonical Bedrock id
    (``...-v1:0``).

    Whether a string is ALREADY qualified is decided by :func:`split_provider_qualifier` — a
    MEMBERSHIP test against :data:`~rebar.llm.config.KNOWN_PROVIDER_NAMES`, never ``":" in model``
    and never a guess at the prefix's shape.

    An explicitly configured ``provider`` is VALIDATED against that same set before anything is
    composed. This is the half membership buys that a shape test cannot: a typo (``bedrok``) is
    reported where the operator made it — during config resolution, which runs in commands that
    never reach an LLM — instead of surviving into a composed target string and failing much later
    at provider construction, if it is reached at all. An unrecognized INLINE qualifier is
    deliberately NOT rejected here; see :func:`split_provider_qualifier`.

    ``endpoint`` is the custom OpenAI-compatible base URL configured for this target (a model
    class slot / fallback candidate ``endpoint``, or a top-level ``base_url``), if any. It only
    affects the bare/inferred ``openai`` family: with an endpoint present that family stays
    ``openai-chat:``; without one it becomes ``openai-responses:`` (ticket 155c). See
    :func:`_openai_target`.
    """
    if provider:
        if provider not in KNOWN_PROVIDER_NAMES:
            raise LLMConfigError(
                f"unknown provider {provider!r} configured for model {model!r}; "
                f"valid providers: {sorted(KNOWN_PROVIDER_NAMES)}"
            )
        qualifier, _ = split_provider_qualifier(model)
        if qualifier == provider or {qualifier, provider} <= {"openai", "openai-chat"}:
            # ``openai`` and ``openai-chat`` name the same provider family. A bare ``openai``
            # request flips to Responses by default (ticket 155c); an explicit ``openai-chat``
            # on EITHER side freezes today's Chat Completions wire contract.
            if {qualifier, provider} <= {"openai", "openai-chat"}:
                tail = model.split(":", 1)[1] if qualifier else model
                if "openai-chat" in {qualifier, provider}:
                    return f"openai-chat:{tail}"
                return _openai_target(tail, endpoint=endpoint)
            return model  # already qualified with the SAME provider — never double-prefix
        if qualifier:
            # A contradiction, not a preference to resolve silently: the config names one provider
            # and the model id names another. Whichever we picked would discard an explicit
            # instruction, which is the defect class this ticket exists to remove.
            raise LLMConfigError(
                f"conflicting provider configuration: provider={provider!r} but the model id "
                f"{model!r} is already qualified for {qualifier!r}. Remove one of them."
            )
        if provider == "openai":
            return _openai_target(model, endpoint=endpoint)
        return f"{provider}:{model}"
    qualifier, _ = split_provider_qualifier(model)
    if qualifier:
        if qualifier == "openai":
            return _openai_target(model.split(":", 1)[1], endpoint=endpoint)
        return model
    inferred = infer_provider(model)
    if inferred == "openai":
        return _openai_target(model, endpoint=endpoint)
    return f"{inferred}:{model}" if inferred else model


def resolve_class(name: str, slots: Mapping[str, ClassSlot]) -> str:
    """The primary ``provider:model`` string for class ``name``. Raises
    :class:`LLMConfigError` if ``name`` is not a known model class."""
    if name not in CLASS_NAMES:
        raise LLMConfigError(f"unknown model class: {name!r} is not one of {CLASS_NAMES}")
    slot = slots[name]
    return _resolve_target(slot.model, slot.provider, endpoint=slot.endpoint)


def resolve_fallback_chain(name: str, slots: Mapping[str, ClassSlot]) -> list[str]:
    """The ordered ``provider:model`` fallback chain for class ``name`` (empty if none
    configured). Order is load-bearing: fallbacks are tried in declaration order. Raises
    :class:`LLMConfigError` if ``name`` is not a known model class."""
    if name not in CLASS_NAMES:
        raise LLMConfigError(f"unknown model class: {name!r} is not one of {CLASS_NAMES}")
    return [_resolve_target(t.model, t.provider, endpoint=t.endpoint) for t in slots[name].fallback]


def load_class_slots(repo_root: str | None = None) -> dict[str, ClassSlot]:
    """Read the real configuration and return the three resolved slots.

    THE integration point: :func:`parse_class_slots` takes an already-loaded mapping, so without
    this entry point the schema would be a parser nothing calls, and every consumer (tasks 172e,
    7761, cc33) would re-invent the extraction. The ``model_classes`` sub-table is pulled out of
    the merged ``[tool.rebar.llm]`` table; a config with no such table yields the documented
    per-class defaults.

    ``_read_llm_file_table`` already merges user < project and already degrades to ``{}`` on a
    malformed core config -- a broken pyproject must never break an LLM op -- so that failure mode
    is INHERITED here rather than re-implemented. Imported inside the function to keep the
    module-level import edge on ``config`` minimal and one-way.
    """
    from rebar.llm.config import _read_llm_file_table

    return parse_class_slots(_read_llm_file_table(repo_root).get("model_classes"))


def resolve_model_string(value: str, repo_root: str | None = None) -> str:
    """Resolve a model string that MAY be a reserved class name.

    The keystone of the class system (decided on eb58): a workflow step names a class by using the
    class name AS its ``model:`` value -- ``model: standard``, ``model_ladder: [trivial, standard,
    frontier]``. There is no ``class:`` key and no schema change; the v3 step schema stays
    closed and only its VALUE space gains three reserved words.

    ``trivial`` / ``standard`` / ``frontier`` resolve through the configured class slots. ANY other
    string is returned UNCHANGED, which is the back-compat guarantee that keeps every existing
    workflow YAML resolving byte-for-byte.

    THERE IS NO SINGLE INTERCEPTION POINT, so this function has several callers rather than one:
    ``model_ladder`` never reaches :func:`rebar.llm.config.resolve_model` (the batch runners copy
    ``model_ladder[0]`` straight onto ``cfg.model``), and neither do the verifier paths
    (``resolve_verifier_model`` / ``_verifier_cfg``). Keeping the reserved vocabulary in THIS one
    function is what stops those call sites from drifting apart.

    Callers in :mod:`rebar.llm.config` must import this lazily INSIDE the function body: this module
    imports ``config`` at module scope, so a module-level import there would close a cycle.
    """
    if value not in CLASS_NAMES:
        return value
    return resolve_class(value, load_class_slots(repo_root))


# ── Fallback chains (task cc33) ───────────────────────────────────────────────
# The runtime half of the schema above: the `fallback` chain parsed into `ClassSlot` is
# consumed here as pydantic-ai's `FallbackModel(default, *fallbacks, fallback_on=...)`.


def should_fall_back(exc: Exception) -> bool:
    """The ``fallback_on`` predicate: should ``exc`` move the chain to its next candidate?

    Deliberately NARROW. pydantic-ai's own docs warn that provider SDK retry logic can delay
    failover substantially — a 429 may be retried inside the SDK (and, for rebar's Anthropic
    path, inside the ``AsyncTenacityTransport``) for up to ~60s before the fallback is ever
    reached — so switching providers is only worth that latency for the AVAILABILITY classes.
    Everything else must fail LOUDLY: a credential error (``CHANGE_SETTINGS``) must not be
    masked by shopping for a provider whose key happens to work; an oversized request
    (``CHANGE_INPUT``) is the size ladder's problem and another provider will not fix it; and
    a quota/payment failure (``INCREASE_PROVIDER_LIMITS`` — which is where a 429 carrying
    ``insufficient_quota`` lands, unlike a plain rate-limit 429) is a SPEND problem, where
    silently relocating spend is worse than stopping.

    Derived from :mod:`rebar.llm.failure`'s taxonomy at CALL time — the existing ``_RETRYABLE``
    frozenset (``WAIT_AND_RETRY`` + ``RETRY_NOW``, the latter covering the down-local-endpoint
    ``httpx.ConnectError`` this feature exists for) plus ``CHANGE_PROVIDER_OR_MODEL`` — rather
    than a hand-maintained tuple of exception types. That is what keeps the two from drifting:
    a future taxonomy change reaches failover without editing this function.
    """
    from rebar.llm.failure import _RETRYABLE, ResolutionClass, classify_llm_failure

    resolution_class = classify_llm_failure(exc).resolution_class
    return (
        resolution_class in _RETRYABLE
        or resolution_class is ResolutionClass.CHANGE_PROVIDER_OR_MODEL
    )


def fallback_targets_for(
    resolved: str,
    slots: Mapping[str, ClassSlot] | None = None,
    *,
    repo_root: str | None = None,
) -> tuple[FallbackTarget, ...]:
    """The ordered fallback chain configured for the class whose PRIMARY resolves to
    ``resolved``, or an empty tuple.

    The runner picks its model as a ``provider:model`` string, so the string is also what
    identifies the slot that string came from — no new plumbing has to thread a class NAME
    through every call site for a chain to be honored. Classes are consulted in
    :data:`CLASS_NAMES` order, so if two slots name the same primary the first wins (their
    chains would otherwise be ambiguous for one and the same model).

    ``repo_root`` is the root the class table is read from when ``slots`` is not supplied; a
    caller holding an ``LLMConfig`` threads ``cfg.repo_path`` so the chain is read from the SAME
    root that config resolved against, rather than from ambient discovery (bug 2876)."""
    slots = load_class_slots(repo_root) if slots is None else slots
    for name in CLASS_NAMES:
        slot = slots.get(name)
        if (
            slot is not None
            and _resolve_target(slot.model, slot.provider, endpoint=slot.endpoint) == resolved
        ):
            return slot.fallback
    return ()


def primary_endpoint_for(
    resolved: str,
    slots: Mapping[str, ClassSlot] | None = None,
    *,
    repo_root: str | None = None,
) -> str | None:
    """The slot-level ``endpoint`` configured for the class whose PRIMARY resolves to
    ``resolved``, or ``None``.

    The companion of :func:`fallback_targets_for` for the PRIMARY model. A slot's ``endpoint``
    (the ``[tool.rebar.llm.model_classes]`` field and ``REBAR_LLM_<CLASS>_ENDPOINT``) is a
    per-class OpenAI-compatible base URL, but ``resolve_class`` returns only a ``provider:model``
    string, so an op collapsing a class onto ``cfg.model`` drops it — leaving the primary with no
    endpoint while ``build_fallback_model`` honors every FALLBACK entry's. This lets the runner
    recover it from the resolved string (the same identity ``fallback_targets_for`` keys on),
    apply it as ``cfg.base_url``, and route the primary through rebar's builder instead of
    pydantic-ai's stock provider (bug 6e70). Classes are consulted in :data:`CLASS_NAMES` order,
    so if two slots name the same primary the first wins. ``repo_root`` is threaded exactly as on
    :func:`fallback_targets_for` — the config's own root, not ambient discovery (bug 2876)."""
    slots = load_class_slots(repo_root) if slots is None else slots
    for name in CLASS_NAMES:
        slot = slots.get(name)
        if (
            slot is not None
            and _resolve_target(slot.model, slot.provider, endpoint=slot.endpoint) == resolved
        ):
            return slot.endpoint
    return None


def build_fallback_model(
    resolved: str, targets: Sequence[FallbackTarget], *, session: Any
) -> tuple[Any, list[str]]:
    """Build the ``FallbackModel`` for primary ``resolved`` plus ``targets``, returning it with
    the ordered ``provider:model`` candidate strings.

    Every candidate is built as a real Model OBJECT through ``session.model_for`` — the same
    provider path the primary takes — rather than handed to ``FallbackModel`` as a string:
    the wrapper's own ``infer_model`` calls carry no ``provider_factory``, so strings would
    bypass rebar's builders entirely and silently drop each entry's ``endpoint``. Sharing one
    session is equally load-bearing: it gives the run a single ``_closeables`` list owning all
    N+1 rebar-created transports, where a session per entry would close only its own."""
    from pydantic_ai.models.fallback import FallbackModel

    candidates = [
        resolved,
        *(_resolve_target(t.model, t.provider, endpoint=t.endpoint) for t in targets),
    ]
    endpoints: list[str | None] = [None, *(t.endpoint for t in targets)]
    models = [
        session.model_for(candidate, endpoint=endpoint)
        for candidate, endpoint in zip(candidates, endpoints, strict=True)
    ]
    from rebar.llm.tracing import wrap_candidate

    models = [
        wrap_candidate(m, i, candidate)
        for i, (m, candidate) in enumerate(zip(models, candidates, strict=True))
    ]
    return FallbackModel(*models, fallback_on=should_fall_back), candidates


def ensure_current_event_loop() -> Any:
    """Return this thread's current event loop, installing one first if absent — WITHOUT
    tripping the Python 3.12+ ``asyncio.get_event_loop()`` "no current event loop"
    ``DeprecationWarning`` (ticket c7d5).

    pydantic-ai's synchronous ``Agent.run_sync`` resolves its loop through
    ``asyncio.get_event_loop()``, whose no-loop fallback is deprecated and warns when nothing
    has installed a loop on the calling thread. Pre-installing the loop the sync run will use
    — the exact loop ``get_event_loop()`` would otherwise lazily create — silences the
    fallback while preserving the current lifecycle: an OPEN already-installed loop is REUSED
    (so a ``FallbackModel`` async-context entry and the subsequent run bind to ONE loop,
    keeping provider HTTP-client loop affinity — upstream pydantic-ai #748), and a fresh loop
    is created only when the thread has none, has one that was explicitly cleared
    (``set_event_loop(None)`` leaves ``get_event_loop`` raising ``RuntimeError``), or has one
    that is already closed. Uses public asyncio API only: promote the deprecation to an error
    solely to detect the "no loop installed" case, never leaking it.
    """
    import asyncio
    import warnings

    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        pass
    with warnings.catch_warnings():
        warnings.simplefilter("error", DeprecationWarning)
        try:
            loop = asyncio.get_event_loop()
            if not loop.is_closed():
                return loop
        except (DeprecationWarning, RuntimeError):
            pass  # no loop installed / explicitly cleared on this thread
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    return loop


@contextmanager
def entered_fallback_model(model: Any) -> Iterator[Any]:
    """Drive ``model``'s async context manager around a SYNCHRONOUS run.

    ``FallbackModel.__aenter__`` enters every sub-model "so their providers can manage HTTP
    client lifecycle" — but ``agent.run_sync`` never enters the model (only ``async with
    agent`` does), so without this the sub-providers pydantic-ai owns are neither entered nor
    closed. Binds the wrapper's entry and the run to ONE loop via
    :func:`ensure_current_event_loop` (the same loop ``run_sync`` resolves through
    ``asyncio.get_event_loop()``). Exit is best-effort, matching ``ProviderSession.close``:
    teardown never raises out over a result."""
    loop = ensure_current_event_loop()
    loop.run_until_complete(model.__aenter__())
    try:
        yield model
    finally:
        try:
            loop.run_until_complete(model.__aexit__(None, None, None))
        except Exception:
            logger.warning("llm fallback chain teardown failed", exc_info=True)


def event_loop_running() -> bool:
    """Is an asyncio event loop RUNNING on the calling thread? (bug f643 —
    ``superior-trifling-dunlin``.)

    Distinct from :func:`ensure_current_event_loop`, which asks "which loop is INSTALLED
    here" and will happily hand back the running one. What the synchronous drive needs to
    know is narrower and load-bearing: ``loop.run_until_complete`` — reached by every LLM
    call rebar makes (``agent.run_sync`` via ``pydantic_ai._utils.get_event_loop``,
    :func:`entered_fallback_model`, and ``ProviderSession``'s ``asyncio.run`` teardown) —
    is legal ONLY on a thread whose loop is not already running, and raises
    ``RuntimeError: This event loop is already running`` when it is.
    """
    import asyncio

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def drive_off_event_loop(fn: Any, *args: Any) -> Any:
    """Run the SYNCHRONOUS ``fn(*args)`` drive on a worker thread with NO running loop.

    Bug f643 (``superior-trifling-dunlin``): under the MCP server a sync ``@mcp.tool`` body
    is invoked DIRECTLY inside the ASGI request coroutine (the python ``mcp`` SDK's
    ``func_metadata`` calls ``fn(**arguments)`` with no thread offload), so the whole tool
    body — including :meth:`rebar.llm.runner.PydanticAIRunner.run` — executes on the event
    loop thread with that loop RUNNING, and every ``run_until_complete`` beneath it raised.
    Handing the drive to a thread of our own restores the invariant the synchronous path was
    always written against; the CLI path (no running loop) never reaches here and is
    unchanged.

    ContextVars are inherited by asyncio tasks but NOT by raw threads, so the callable is
    dispatched through a FRESH ``contextvars.copy_context()`` — the same pattern (and for
    the same reason) as ``plan_review.generation._submit_ctx``. Without it the gate-session
    vars (``_in_gate_session`` / ``_active_code_root`` / ``_active_tickets_root`` /
    ``_active_gate_config``) and the review cancel scope would be absent in the worker and
    ``assert_gated`` would fail closed on every agentic run. A fresh copy per call because
    one ``Context`` cannot be entered concurrently.

    The pool is a context manager, so the worker is joined before this returns — the thread
    can never outlive the call that spawned it. ``Future.result()`` re-raises the worker's
    exception object itself, so type, message and traceback reach the caller unchanged.
    """
    import contextvars
    from concurrent.futures import ThreadPoolExecutor

    context = contextvars.copy_context()
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="rebar-llm-drive") as pool:
        return pool.submit(context.run, fn, *args).result()
