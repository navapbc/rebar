"""Typed ``Mutation.payload`` contracts — one dataclass per live ``(direction,
action)`` pair (ADR 0107).

``rebar_reconciler.mutation.Mutation`` already types its ``direction``/``action``
fields and constrains valid combinations via ``_VALID_COMBINATIONS``; what remains
untyped is ``Mutation.payload: Mapping[str, Any]``. This module closes that gap
with ten frozen, ``Mapping``-compatible payload dataclasses (one per combination
``_VALID_COMBINATIONS`` allows AND ``typed_dispatch._LEAVES`` actually registers —
``(inbound, delete)``/``(inbound, probe)`` are dead-by-design per ADR 0028/bug 3b5f
and are deliberately NOT modeled here; feeding either to :func:`build_typed_payload`
raises :class:`UnknownMutationKindError`).

Each payload type is:

* a ``Mapping[str, Any]`` (via the ``_PayloadMapping`` mixin, keyed by
  ``as_legacy_dict()``) — so a typed payload is a drop-in replacement for the
  ``dict`` payload ``Mutation.__post_init__`` already accepts; no change to
  ``Mutation`` itself is required (the ADR's "Expand" migration step).
* validated in ``__post_init__``/``from_legacy`` — missing required fields,
  wrong-action fields, or unrecognized ("extra critical") fields raise
  ``ValueError``/``TypeError`` before any effect runs (AC1).
* round-trippable through ``as_legacy_dict()`` back to the exact historical
  dict shape for every already-shipped ``(direction, action, target)`` triple,
  so ``serialize_manifest``'s sha256 (an external compatibility surface) is
  unchanged when a typed payload is substituted for its legacy dict twin.

This module intentionally does NOT change any producer (``differ.py``,
``outbound_mutation_builders.py``, ``run_differs.py``/``outbound_pass.py``,
``binding_walk.py``, ``invariants.py``): per AC7, production continues
constructing legacy dict payloads in this story. ``build_typed_payload`` is the
one new entry point, consumed only by the side-effect-free shadow comparator
(``payload_shadow.py``) and its tests. Wiring producers to construct these types
directly is the ADR's "Cut" step, explicitly deferred.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any


class UnknownMutationKindError(ValueError):
    """Raised by :func:`build_typed_payload` for a ``(direction, action)`` pair
    with no registered payload type — includes the two dead-by-design inbound
    combinations (``delete``/``probe``), which are deliberately unregistered."""


def _as_tuple_of_mappings(value: Any) -> tuple[Mapping[str, Any], ...]:
    if not value:
        return ()
    return tuple(dict(item) for item in value)


class _PayloadMapping(Mapping[str, Any]):
    """Shared ``Mapping`` projection for every payload dataclass below.

    Keys/values come from :meth:`as_legacy_dict` — the single place each
    subclass declares its compatibility-bytes shape. Subclassing ``Mapping``
    (not merely duck-typing it) satisfies ``Mutation.__post_init__``'s
    ``isinstance(self.payload, Mapping)`` guard unchanged.
    """

    def as_legacy_dict(self) -> dict[str, Any]:  # pragma: no cover - overridden
        raise NotImplementedError

    @classmethod
    def from_legacy(
        cls, payload: Mapping[str, Any]
    ) -> _PayloadMapping:  # pragma: no cover - overridden
        raise NotImplementedError

    def __getitem__(self, key: str) -> Any:
        return self.as_legacy_dict()[key]

    def __iter__(self):
        return iter(self.as_legacy_dict())

    def __len__(self) -> int:
        return len(self.as_legacy_dict())

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"{type(self).__name__}({self.as_legacy_dict()!r})"


# ---------------------------------------------------------------------------
# Outbound payloads
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class OutboundCreatePayload(_PayloadMapping):
    """``(outbound, create)`` — vendor create fields plus post-creation
    resolution data. Mirrors ``outbound_pass.py``'s
    ``{**om.fields, "comments": ..., "labels": ..., "local_id": ...}`` shape."""

    fields: Mapping[str, Any]
    comments: tuple[Mapping[str, Any], ...] = ()
    labels: tuple[Mapping[str, Any], ...] = ()
    links: tuple[Mapping[str, Any], ...] = ()
    local_id: str | None = None
    key_hint: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.fields, Mapping):
            raise TypeError("OutboundCreatePayload.fields must be a Mapping")

    def as_legacy_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = dict(self.fields)
        out["comments"] = [dict(c) for c in self.comments]
        out["labels"] = [dict(entry) for entry in self.labels]
        if self.links:
            out["links"] = [dict(entry) for entry in self.links]
        if self.local_id is not None:
            out["local_id"] = self.local_id
        if self.key_hint is not None:
            out["key_hint"] = self.key_hint
        return out

    @classmethod
    def from_legacy(cls, payload: Mapping[str, Any]) -> OutboundCreatePayload:
        remainder = dict(payload)
        comments = _as_tuple_of_mappings(remainder.pop("comments", None))
        labels = _as_tuple_of_mappings(remainder.pop("labels", None))
        links = _as_tuple_of_mappings(remainder.pop("links", None))
        local_id = remainder.pop("local_id", None)
        key_hint = remainder.pop("key_hint", None)
        # Everything else is the vendor create field spread — intentionally
        # open-ended (no allowlist): CREATE fields vary per project/mapping.
        return cls(
            fields=remainder,
            comments=comments,
            labels=labels,
            links=links,
            local_id=local_id,
            key_hint=key_hint,
        )


_OUTBOUND_UPDATE_KEYS = frozenset({"changed_fields", "comments", "labels", "links", "local_id"})


@dataclass(frozen=True)
class OutboundUpdatePayload(_PayloadMapping):
    """``(outbound, update)`` — field/comment/label/link diffs for a bound
    ticket. Mirrors ``outbound_pass.py``'s
    ``{"changed_fields": ..., "comments": ..., "labels": ..., "links": ...}``."""

    changed_fields: Mapping[str, Any] = field(default_factory=dict)
    comments: tuple[Mapping[str, Any], ...] = ()
    labels: tuple[Mapping[str, Any], ...] = ()
    links: tuple[Mapping[str, Any], ...] = ()
    local_id: str | None = None

    def __post_init__(self) -> None:
        if not (self.changed_fields or self.comments or self.labels or self.links):
            raise ValueError(
                "OutboundUpdatePayload requires at least one of "
                "changed_fields/comments/labels/links to be non-empty"
            )

    def as_legacy_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "changed_fields": dict(self.changed_fields),
            "comments": [dict(c) for c in self.comments],
            "labels": [dict(entry) for entry in self.labels],
            "links": [dict(entry) for entry in self.links],
        }
        if self.local_id is not None:
            out["local_id"] = self.local_id
        return out

    @classmethod
    def from_legacy(cls, payload: Mapping[str, Any]) -> OutboundUpdatePayload:
        extra = set(payload) - _OUTBOUND_UPDATE_KEYS
        if extra:
            raise ValueError(f"OutboundUpdatePayload: unrecognized fields {sorted(extra)}")
        return cls(
            changed_fields=dict(payload.get("changed_fields") or {}),
            comments=_as_tuple_of_mappings(payload.get("comments")),
            labels=_as_tuple_of_mappings(payload.get("labels")),
            links=_as_tuple_of_mappings(payload.get("links")),
            local_id=payload.get("local_id"),
        )


@dataclass(frozen=True)
class OutboundDeletePayload(_PayloadMapping):
    """``(outbound, delete)`` — the Jira key lives in ``Mutation.target``; the
    payload itself carries no fields at all (a nonempty payload is rejected)."""

    def as_legacy_dict(self) -> dict[str, Any]:
        return {}

    @classmethod
    def from_legacy(cls, payload: Mapping[str, Any]) -> OutboundDeletePayload:
        if payload:
            raise ValueError(f"OutboundDeletePayload accepts no fields, got {sorted(payload)}")
        return cls()


@dataclass(frozen=True)
class OutboundProbePayload(_PayloadMapping):
    """``(outbound, probe)`` — the ambiguity reason lives in
    ``Mutation.provenance``; the payload carries no fields."""

    def as_legacy_dict(self) -> dict[str, Any]:
        return {}

    @classmethod
    def from_legacy(cls, payload: Mapping[str, Any]) -> OutboundProbePayload:
        if payload:
            raise ValueError(f"OutboundProbePayload accepts no fields, got {sorted(payload)}")
        return cls()


@dataclass(frozen=True)
class OutboundConflictPayload(_PayloadMapping):
    """``(outbound, conflict)`` — a human-readable reason, optionally scoped
    to a local id."""

    reason: str
    local_id: str | None = None

    def __post_init__(self) -> None:
        if not self.reason:
            raise ValueError("OutboundConflictPayload.reason must be a non-empty str")

    def as_legacy_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"reason": self.reason}
        if self.local_id is not None:
            out["local_id"] = self.local_id
        return out

    @classmethod
    def from_legacy(cls, payload: Mapping[str, Any]) -> OutboundConflictPayload:
        extra = set(payload) - {"reason", "local_id"}
        if extra:
            raise ValueError(f"OutboundConflictPayload: unrecognized fields {sorted(extra)}")
        if "reason" not in payload:
            raise ValueError("OutboundConflictPayload requires 'reason'")
        return cls(reason=payload["reason"], local_id=payload.get("local_id"))


# ---------------------------------------------------------------------------
# Inbound payloads
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class InboundCreatePayload(_PayloadMapping):
    """``(inbound, create)`` — jira-shape scalar fields for a new local
    ticket. Mirrors ``differ.py``'s flat
    ``{f: v for f, v in jira_fields.items() if f not in excluded}`` shape."""

    fields: Mapping[str, Any]
    status: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.fields, Mapping):
            raise TypeError("InboundCreatePayload.fields must be a Mapping")

    def as_legacy_dict(self) -> dict[str, Any]:
        out = dict(self.fields)
        if self.status is not None:
            out["status"] = self.status
        return out

    @classmethod
    def from_legacy(cls, payload: Mapping[str, Any]) -> InboundCreatePayload:
        remainder = dict(payload)
        status = remainder.pop("status", None)
        return cls(fields=remainder, status=status)


_INBOUND_UPDATE_KEYS = frozenset({"local_id", "fields", "status", "labels", "comments", "links"})


@dataclass(frozen=True)
class InboundUpdatePayload(_PayloadMapping):
    """``(inbound, update)`` — field/label/comment/link updates for a bound
    local ticket. Mirrors ``run_differs.py``'s
    ``{"local_id": ..., "fields": ..., "labels": ..., "comments": ..., "links": ...}``."""

    fields: Mapping[str, Any] = field(default_factory=dict)
    local_id: str | None = None
    status: str | None = None
    labels: tuple[Mapping[str, Any], ...] = ()
    comments: tuple[Mapping[str, Any], ...] = ()
    links: tuple[Mapping[str, Any], ...] = ()

    def as_legacy_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "local_id": self.local_id,
            "fields": dict(self.fields),
            "labels": [dict(entry) for entry in self.labels],
            "comments": [dict(c) for c in self.comments],
            "links": [dict(entry) for entry in self.links],
        }
        if self.status is not None:
            out["status"] = self.status
        return out

    @classmethod
    def from_legacy(cls, payload: Mapping[str, Any]) -> InboundUpdatePayload:
        extra = set(payload) - _INBOUND_UPDATE_KEYS
        if extra:
            raise ValueError(f"InboundUpdatePayload: unrecognized fields {sorted(extra)}")
        return cls(
            fields=dict(payload.get("fields") or {}),
            local_id=payload.get("local_id"),
            status=payload.get("status"),
            labels=_as_tuple_of_mappings(payload.get("labels")),
            comments=_as_tuple_of_mappings(payload.get("comments")),
            links=_as_tuple_of_mappings(payload.get("links")),
        )


@dataclass(frozen=True)
class InboundCleanLabelPayload(_PayloadMapping):
    """``(inbound, clean_label)`` — stale ``rebar-id-*`` labels to remove.
    Mirrors ``apply_inbound.py``'s ``payload["labels_to_remove"]`` read."""

    labels_to_remove: tuple[str, ...]

    def __post_init__(self) -> None:
        if not self.labels_to_remove:
            raise ValueError("InboundCleanLabelPayload.labels_to_remove must be non-empty")

    def as_legacy_dict(self) -> dict[str, Any]:
        return {"labels_to_remove": list(self.labels_to_remove)}

    @classmethod
    def from_legacy(cls, payload: Mapping[str, Any]) -> InboundCleanLabelPayload:
        extra = set(payload) - {"labels_to_remove"}
        if extra:
            raise ValueError(f"InboundCleanLabelPayload: unrecognized fields {sorted(extra)}")
        labels = payload.get("labels_to_remove")
        if not labels:
            raise ValueError("InboundCleanLabelPayload requires non-empty 'labels_to_remove'")
        return cls(labels_to_remove=tuple(labels))


@dataclass(frozen=True)
class InboundRepairPropertyPayload(_PayloadMapping):
    """``(inbound, repair_property)`` — the local id to write back onto the
    Jira issue's entity property. Mirrors ``invariants.py``'s
    ``payload={"local_id": rebar_id}``."""

    local_id: str

    def __post_init__(self) -> None:
        if not self.local_id:
            raise ValueError("InboundRepairPropertyPayload.local_id must be a non-empty str")

    def as_legacy_dict(self) -> dict[str, Any]:
        return {"local_id": self.local_id}

    @classmethod
    def from_legacy(cls, payload: Mapping[str, Any]) -> InboundRepairPropertyPayload:
        extra = set(payload) - {"local_id"}
        if extra:
            raise ValueError(f"InboundRepairPropertyPayload: unrecognized fields {sorted(extra)}")
        if "local_id" not in payload or not payload["local_id"]:
            raise ValueError("InboundRepairPropertyPayload requires non-empty 'local_id'")
        return cls(local_id=payload["local_id"])


@dataclass(frozen=True)
class InboundConflictPayload(_PayloadMapping):
    """``(inbound, conflict)`` — a human-readable reason plus the Jira key it
    concerns, per the ADR 0107 decision table.

    NOTE (named delta — see ``docs/adr/0107-...md`` Decision §1 and the e9d5
    shadow-corpus comparison): today's live ``differ.py`` inbound-conflict
    producers put ``reason`` in ``Mutation.provenance`` (not ``payload``) and
    often ship an empty payload (or one carrying ``jira_field_snapshot``) — see
    ``test_payload_shadow_corpus.py::test_inbound_conflict_legacy_shape_is_an_
    approved_delta``. This type intentionally encodes the ADR's DECIDED contract
    (not today's producer shape); reconciling producers to it is deferred to the
    ADR's "Cut" step.
    """

    reason: str
    jira_key: str
    local_id: str | None = None

    def __post_init__(self) -> None:
        if not self.reason:
            raise ValueError("InboundConflictPayload.reason must be a non-empty str")
        if not self.jira_key:
            raise ValueError("InboundConflictPayload.jira_key must be a non-empty str")

    def as_legacy_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"reason": self.reason, "jira_key": self.jira_key}
        if self.local_id is not None:
            out["local_id"] = self.local_id
        return out

    @classmethod
    def from_legacy(cls, payload: Mapping[str, Any]) -> InboundConflictPayload:
        extra = set(payload) - {"reason", "jira_key", "local_id"}
        if extra:
            raise ValueError(f"InboundConflictPayload: unrecognized fields {sorted(extra)}")
        if "reason" not in payload or "jira_key" not in payload:
            raise ValueError("InboundConflictPayload requires 'reason' and 'jira_key'")
        return cls(
            reason=payload["reason"], jira_key=payload["jira_key"], local_id=payload.get("local_id")
        )


# ---------------------------------------------------------------------------
# Registry / factory
# ---------------------------------------------------------------------------

# Exactly the 10 LIVE (direction, action) combinations — deliberately excludes
# (inbound, delete) and (inbound, probe), which typed_dispatch._LEAVES never
# registers (dead by design, ADR 0028 / bug 3b5f). Keyed by
# (direction.value, action.value) string pairs so this module has no import-time
# dependency on mutation.py's enum classes (which are dynamically loaded under a
# canonical sys.modules key elsewhere in this package — see ADR 0083) — avoiding
# any cross-module enum-identity concern for this purely-additive contract layer.
_PAYLOAD_TYPES: dict[tuple[str, str], type[_PayloadMapping]] = {
    ("outbound", "create"): OutboundCreatePayload,
    ("outbound", "update"): OutboundUpdatePayload,
    ("outbound", "delete"): OutboundDeletePayload,
    ("outbound", "probe"): OutboundProbePayload,
    ("outbound", "conflict"): OutboundConflictPayload,
    ("inbound", "create"): InboundCreatePayload,
    ("inbound", "update"): InboundUpdatePayload,
    ("inbound", "clean_label"): InboundCleanLabelPayload,
    ("inbound", "repair_property"): InboundRepairPropertyPayload,
    ("inbound", "conflict"): InboundConflictPayload,
}


def payload_type_for(direction: str, action: str) -> type[_PayloadMapping]:
    """Return the payload dataclass for a live ``(direction, action)`` pair.

    Raises :class:`UnknownMutationKindError` for any pair not in the 10 live
    combinations — including the two dead-by-design inbound pairs.
    """
    key = (str(direction), str(action))
    try:
        return _PAYLOAD_TYPES[key]
    except KeyError:
        raise UnknownMutationKindError(
            f"no typed payload for (direction={direction!s}, action={action!s})"
        ) from None


def build_typed_payload(direction: str, action: str, payload: Mapping[str, Any]) -> _PayloadMapping:
    """Construct the named payload type for ``(direction, action)`` from a
    legacy dict-shaped ``payload``.

    Raises before any effect: :class:`UnknownMutationKindError` for an
    unregistered pair, ``ValueError``/``TypeError`` for missing required
    fields, wrong-action fields, or unrecognized ("extra critical") fields —
    this is AC1's "construction rejects ... before effects".
    """
    payload_cls = payload_type_for(direction, action)
    return payload_cls.from_legacy(payload)


def as_legacy_dict(payload: Any) -> dict[str, Any]:
    """Project any ``Mutation.payload`` value back to its legacy dict shape.

    Typed payloads (``_PayloadMapping`` subclasses) delegate to their own
    ``as_legacy_dict()``; a plain ``Mapping`` (the pre-existing untyped-dict
    shape) is copied via ``dict(...)`` unchanged. This is the ONE new
    responsibility ``serialize_manifest`` gains (ADR 0107 Decision §2) so the
    emitted JSON bytes for an already-shipped triple are unaffected by whether
    its payload happens to be typed or a raw dict.
    """
    projector = getattr(payload, "as_legacy_dict", None)
    if callable(projector):
        return projector()
    return dict(payload)
