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

Cross-sibling types (``ObservationVersion``) are loaded by file path via the
package's shared ``lazy_load`` idiom (``_loader.py``), which resolves both under
the real package and when this module is exec'd standalone in tests. Absolute
imports are used for the ``rebar`` package where needed.
"""

from __future__ import annotations

import enum
import importlib.util
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

try:
    from rebar_reconciler._loader import lazy_load
except ImportError:  # standalone load without package context
    _loader_key = "rebar_reconciler._loader"
    if _loader_key not in sys.modules:
        _loader_spec = importlib.util.spec_from_file_location(
            _loader_key, Path(__file__).parent / "_loader.py"
        )
        assert _loader_spec is not None and _loader_spec.loader is not None
        _loader_mod = importlib.util.module_from_spec(_loader_spec)
        sys.modules[_loader_key] = _loader_mod
        _loader_spec.loader.exec_module(_loader_mod)
    lazy_load = sys.modules[_loader_key].lazy_load

_observation = lazy_load("rebar_reconciler.observation", "observation.py")
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


@dataclass(frozen=True, slots=True, eq=False)
class TicketPlan:
    """A frozen, per-ticket plan derived purely from one pass's observation."""

    identity: str
    mutations: tuple[Any, ...]
    diagnostics: tuple[str, ...]
    disposition: PlanDisposition
    observation_version: Any
    payload: Mapping[str, Any]

    def __post_init__(self) -> None:
        # Normalize to tuples defensively so callers passing lists still get a
        # frozen, hashable sequence, and deep-freeze the payload mapping.
        object.__setattr__(self, "mutations", tuple(self.mutations))
        object.__setattr__(self, "diagnostics", tuple(self.diagnostics))
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
            )
        )
