"""Pure, per-ticket plan value produced by the shadow planner.

A ``TicketPlan`` is the immutable, deterministic plan for a single ticket
identity: the ``Mutation`` values targeting it, human-readable ``diagnostics``,
a ``PlanDisposition``, the ``observation_version`` it was derived from, and an
open, deep-frozen provider-specific ``payload``. Construction is PURE — no I/O,
no clock.

``__eq__`` compares ALL fields (payload included); ``__hash__`` covers only the
hashable subset ``(identity, mutations, diagnostics, disposition,
observation_version)`` because ``payload`` is often a dict — mirroring the
identity/hash split in ``mutation.py``'s ``Mutation``.

Cross-sibling types (``ObservationVersion``) are imported lazily as plain
``rebar.reconciler`` modules. Absolute
imports are used for the ``rebar`` package where needed.
"""

from __future__ import annotations

import enum
import importlib.util
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

_observation = importlib.import_module("rebar.reconciler.observation")
ObservationVersion = _observation.ObservationVersion
_deep_freeze = _observation._deep_freeze


class PlanDisposition(str, enum.Enum):
    """What the planner decided for a ticket.

    T1 only ever emits ``mutate``; ``defer`` and ``noop`` are the reserved
    forward-compat surface that later S2 tasks (lifecycle intents / scope
    deferral) extend.
    """

    mutate = "mutate"
    defer = "defer"
    noop = "noop"


class IntentKind(str, enum.Enum):
    """Provider-neutral lifecycle intent kinds a mutate plan can derive."""

    bind = "bind"
    confirm = "confirm"
    rekey = "rekey"
    retire = "retire"
    baseline = "baseline"
    comment_identity = "comment_identity"


class DeferReason(str, enum.Enum):
    """Why a plan was deferred/skipped — names/values mirror the four provider-neutral
    ``operation_outcome.Disposition`` members the shadow planner can produce."""

    dependency_deferred = "dependency_deferred"
    scope_deferred = "scope_deferred"
    safety_aborted = "safety_aborted"
    skipped = "skipped"


@dataclass(frozen=True, slots=True, eq=False)
class LifecycleIntent:
    """A frozen, provider-neutral lifecycle intent derived from one mutation.

    ``__eq__`` compares all fields (payload included); ``__hash__`` excludes the
    unhashable ``payload`` mapping — mirroring the ``TicketPlan`` identity/hash split.
    """

    kind: IntentKind
    target: str
    version: Any
    payload: Mapping[str, Any]

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", _deep_freeze(self.payload))

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, LifecycleIntent):
            return NotImplemented
        return (
            self.kind == other.kind
            and self.target == other.target
            and self.version == other.version
            and self.payload == other.payload
        )

    def __hash__(self) -> int:
        return hash((self.kind, self.target, self.version))


@dataclass(frozen=True, slots=True)
class ParityDelta:
    """One bounded, structured difference between the legacy mutation list and the
    shadow plans for a single identity.

    ``fields`` carries ONLY bounded, structured strings (direction/action pairs or
    ``disposition=``/``defer_reason=`` markers) — never a raw mutation payload or
    provenance, which may contain secrets (AC7)."""

    identity: str
    observation_version: Any
    kind: str
    fields: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ParityReport:
    """The result of comparing shadow plans against the legacy mutation list.

    ``deltas`` is every observed difference; ``unexpected`` is the subset whose
    identity is not pre-approved; ``matched`` is ``True`` exactly when
    ``unexpected`` is empty."""

    deltas: tuple[ParityDelta, ...]
    unexpected: tuple[ParityDelta, ...]
    matched: bool


@dataclass(frozen=True, slots=True, eq=False)
class TicketPlan:
    """A frozen, per-ticket plan derived purely from one pass's observation."""

    identity: str
    mutations: tuple[Any, ...]
    diagnostics: tuple[str, ...]
    disposition: PlanDisposition
    observation_version: Any
    payload: Mapping[str, Any]
    intents: tuple[Any, ...] = ()
    dependencies: tuple[str, ...] = ()
    defer_reason: DeferReason | None = None

    def __post_init__(self) -> None:
        # Normalize to tuples defensively so callers passing lists still get a
        # frozen, hashable sequence, and deep-freeze the payload mapping.
        object.__setattr__(self, "mutations", tuple(self.mutations))
        object.__setattr__(self, "diagnostics", tuple(self.diagnostics))
        object.__setattr__(self, "intents", tuple(self.intents))
        object.__setattr__(self, "dependencies", tuple(self.dependencies))
        object.__setattr__(self, "payload", _deep_freeze(self.payload))

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, TicketPlan):
            return NotImplemented
        return (
            self.identity == other.identity
            and self.mutations == other.mutations
            and self.diagnostics == other.diagnostics
            and self.disposition == other.disposition
            and self.observation_version == other.observation_version
            and self.payload == other.payload
            and self.intents == other.intents
            and self.dependencies == other.dependencies
            and self.defer_reason == other.defer_reason
        )

    def __hash__(self) -> int:
        # payload is often a dict (unhashable) and is excluded from the hash but
        # included in __eq__ — mirroring Mutation's identity/hash split.
        return hash(
            (
                self.identity,
                self.mutations,
                self.diagnostics,
                self.disposition,
                self.observation_version,
                self.intents,
                self.dependencies,
                self.defer_reason,
            )
        )
