"""Immutable, provider-neutral observation of one reconcile pass's inputs.

An ``Observation`` is a frozen snapshot of the SUBSTANTIVE inputs to a single
reconcile pass — the local/remote snapshots, the binding view, the target mode,
the selection, the limits, and an open provider-specific ``payload`` — plus an
``ObservationVersion`` identity. Construction is PURE: no I/O, no clock, no
subprocess. Every Mapping field is DEEP-FROZEN (recursively wrapped in
``types.MappingProxyType`` over a deep copy) and defensively copied from the
caller's inputs, so mutating the caller's dict after ``build_observation`` cannot
change the Observation.

``build_observation`` derives the version ``fingerprint`` as ``content_hash`` over
the provider-NEUTRAL substantive inputs (everything except ``pass_id`` AND the open
``payload`` extension channel), so two passes over identical substantive data share a
fingerprint — regardless of any provider payload — while ``pass_id`` still
distinguishes their version identity.

This module uses NORMAL absolute imports for the ``rebar`` package (matching
``mutation.py``); cross-sibling reconciler types are not needed here.
"""

from __future__ import annotations

import copy
import types
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from rebar._store.canonical import content_hash


def _deep_freeze(value: Any) -> Any:
    """Recursively deep-copy ``value`` and wrap every Mapping in a read-only
    ``MappingProxyType`` so nested dicts are also immutable. Non-mapping
    containers (lists/tuples) have their elements frozen too."""
    if isinstance(value, Mapping):
        return types.MappingProxyType({k: _deep_freeze(v) for k, v in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_deep_freeze(v) for v in value)
    return copy.deepcopy(value)


@dataclass(frozen=True, slots=True)
class ObservationVersion:
    """Frozen, hashable identity of one observation: the pass that produced it
    (``pass_id``) plus a ``fingerprint`` over its substantive inputs."""

    pass_id: str
    fingerprint: str


@dataclass(frozen=True, slots=True, eq=False)
class Observation:
    """A frozen, provider-neutral snapshot of one reconcile pass's inputs.

    Every Mapping field is deep-frozen and defensively copied. Equality is
    structural (all fields compared). Not required to be hashable.
    """

    version: ObservationVersion
    local_snapshot: Mapping[str, Any]
    remote_snapshot: Mapping[str, Any]
    binding_view: Mapping[str, Any]
    mode: str
    selection: Mapping[str, Any]
    limits: Mapping[str, Any]
    payload: Mapping[str, Any] = field(default_factory=dict)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Observation):
            return NotImplemented
        return (
            self.version == other.version
            and self.local_snapshot == other.local_snapshot
            and self.remote_snapshot == other.remote_snapshot
            and self.binding_view == other.binding_view
            and self.mode == other.mode
            and self.selection == other.selection
            and self.limits == other.limits
            and self.payload == other.payload
        )


def build_observation(
    *,
    pass_id: str,
    local_snapshot: Mapping[str, Any],
    remote_snapshot: Mapping[str, Any],
    binding_view: Mapping[str, Any],
    mode: str,
    selection: Mapping[str, Any],
    limits: Mapping[str, Any],
    payload: Mapping[str, Any] | None = None,
) -> Observation:
    """Build a frozen ``Observation`` purely from the given pass inputs.

    The version ``fingerprint`` is ``content_hash`` over the substantive inputs
    only (NOT ``pass_id``), so identical data yields an identical fingerprint.
    ``payload=None`` is treated as an empty mapping.
    """
    payload = payload if payload is not None else {}
    # The fingerprint identifies the provider-NEUTRAL core of the pass. ``payload`` is
    # an OPEN provider-specific extension channel (AC5): it must not alter the core
    # identity, so two passes over identical substantive data share a fingerprint
    # regardless of any provider payload. It is therefore excluded here.
    substantive = {
        "local_snapshot": dict(local_snapshot),
        "remote_snapshot": dict(remote_snapshot),
        "binding_view": dict(binding_view),
        "mode": mode,
        "selection": dict(selection),
        "limits": dict(limits),
    }
    fingerprint = content_hash(substantive)
    version = ObservationVersion(pass_id=pass_id, fingerprint=fingerprint)
    return Observation(
        version=version,
        local_snapshot=_deep_freeze(local_snapshot),
        remote_snapshot=_deep_freeze(remote_snapshot),
        binding_view=_deep_freeze(binding_view),
        mode=mode,
        selection=_deep_freeze(selection),
        limits=_deep_freeze(limits),
        payload=_deep_freeze(payload),
    )
