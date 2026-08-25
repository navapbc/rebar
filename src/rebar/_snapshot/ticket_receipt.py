"""Typed completion read bases and descendant-safe ticket receipt validation."""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Protocol

_OID_RE = re.compile(r"^[0-9a-f]{40,64}$")
_GIT_TIMEOUT_SECONDS = 30


class _ReceiptView(Protocol):
    """The exact pinned-view operations a persisted read receipt can replay."""

    def resolve(self, ticket_ref: str) -> str | None: ...

    def show_ticket(self, ticket_ref: str) -> Mapping[str, object]: ...

    def field_observation(self, ticket_ref: str, field: str) -> Mapping[str, object]: ...

    def direct_child_ids(self, ticket_ref: str) -> list[str]: ...

    def transitive_descendant_ids(self, ticket_ref: str) -> list[str]: ...

    def inbound_links(self, ticket_ref: str) -> list[tuple[str, str, str]]: ...

    def relation_reachable(
        self, source_ref: str, target_ref: str, *, relations: Iterable[str]
    ) -> bool: ...


def _mapping(value: object) -> Mapping[object, object]:
    return value if isinstance(value, Mapping) else {}


def _dependencies(state: Mapping[str, object]) -> tuple[Mapping[object, object], ...]:
    raw = state.get("deps")
    if not isinstance(raw, list):
        return ()
    return tuple(dep for dep in raw if isinstance(dep, Mapping))


def _freeze(value: object) -> object:
    if isinstance(value, Mapping):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, (list, tuple)):
        return tuple(_freeze(item) for item in value)
    return value


def _freeze_mapping(value: Mapping[str, object]) -> Mapping[str, object]:
    return MappingProxyType({key: _freeze(item) for key, item in value.items()})


def _thaw(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw(item) for item in value]
    return value


def _thaw_mapping(value: Mapping[str, object]) -> dict[str, object]:
    return {key: _thaw(item) for key, item in value.items()}


def _digest(value: object) -> str:
    encoded = json.dumps(_thaw(value), sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


def _receipt_schema_matches(receipt: Mapping[str, object]) -> bool:
    from rebar.reducer import SCHEMA_VERSION

    return (
        receipt.get("schema") == "ticket_read_receipt_v1"
        and receipt.get("view_schema_version") == 1
        and receipt.get("reducer_schema_version") == SCHEMA_VERSION
    )


# raw-git-ok: bounded read-only receipt/object helper; every semantic caller was censused
def _run_git(
    tracker: str,
    *args: str,
    input_bytes: bytes | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[bytes]:
    from rebar._snapshot.ticket_view import PinnedTicketViewError

    try:
        proc = subprocess.run(
            ["git", "-C", tracker, *args],
            input=input_bytes,
            capture_output=True,
            check=False,
            timeout=_GIT_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        raise PinnedTicketViewError(
            f"git {' '.join(args)} timed out after {_GIT_TIMEOUT_SECONDS}s"
        ) from None
    if check and proc.returncode != 0:
        detail = proc.stderr.decode(errors="replace").strip()
        raise PinnedTicketViewError(f"git {' '.join(args)} failed: {detail}")
    return proc


@dataclass(frozen=True)
class CodeOID:
    value: str

    def __post_init__(self) -> None:
        if not _OID_RE.fullmatch(self.value):
            raise ValueError("code OID must be a full hexadecimal Git object id")


@dataclass(frozen=True)
class TicketsOID:
    value: str

    def __post_init__(self) -> None:
        if not _OID_RE.fullmatch(self.value):
            raise ValueError("tickets OID must be a full hexadecimal Git object id")


@dataclass(frozen=True)
class CompletionReadBasis:
    run_id: str
    code_oid: CodeOID
    tickets_oid: TicketsOID
    receipt: Mapping[str, object]
    receipt_digest: str

    def __post_init__(self) -> None:
        if not isinstance(self.code_oid, CodeOID) or not isinstance(self.tickets_oid, TicketsOID):
            raise TypeError("code_oid and tickets_oid are distinct typed handles")
        if not _receipt_schema_matches(self.receipt):
            raise ValueError("unsupported ticket read receipt schema")
        if self.receipt.get("tickets_oid") != self.tickets_oid.value:
            raise ValueError("completion read basis and receipt use different tickets OIDs")
        if self.receipt_digest != _digest(self.receipt):
            raise ValueError("completion read receipt digest does not match its payload")
        object.__setattr__(self, "receipt", _freeze_mapping(self.receipt))

    def to_dict(self) -> dict[str, object]:
        return {
            "schema": "completion_read_basis_v1",
            "run_id": self.run_id,
            "code_oid": self.code_oid.value,
            "tickets_oid": self.tickets_oid.value,
            "receipt": _thaw_mapping(self.receipt),
            "receipt_digest": self.receipt_digest,
        }

    @classmethod
    def from_dict(cls, raw: Mapping[str, object]) -> CompletionReadBasis:
        """Parse the durable wire shape and reject swapped, truncated, or altered bases."""
        if raw.get("schema") != "completion_read_basis_v1":
            raise ValueError("unsupported completion read basis schema")
        run_id = raw.get("run_id")
        receipt_value = raw.get("receipt")
        digest = raw.get("receipt_digest")
        if not isinstance(run_id, str) or not run_id:
            raise ValueError("completion read basis run_id must be non-empty")
        if not isinstance(receipt_value, Mapping):
            raise ValueError("completion read basis receipt must be an object")
        receipt = {str(key): value for key, value in receipt_value.items()}
        if not _receipt_schema_matches(receipt):
            raise ValueError("unsupported ticket read receipt schema")
        if not isinstance(digest, str) or digest != _digest(receipt):
            raise ValueError("completion read receipt digest does not match its payload")
        code_oid = CodeOID(str(raw.get("code_oid", "")))
        tickets_oid = TicketsOID(str(raw.get("tickets_oid", "")))
        if receipt.get("tickets_oid") != tickets_oid.value:
            raise ValueError("completion read basis and receipt use different tickets OIDs")
        return cls(
            run_id=run_id,
            code_oid=code_oid,
            tickets_oid=tickets_oid,
            receipt=_thaw_mapping(receipt),
            receipt_digest=digest,
        )


@dataclass(frozen=True)
class ReceiptValidation:
    valid: bool
    current_oid: TicketsOID
    conflicts: tuple[str, ...] = ()


def tracker_head(tracker: str) -> TicketsOID:
    out = _run_git(tracker, "rev-parse", "HEAD").stdout.decode().strip()
    return TicketsOID(out)


def _compare_ticket_observations(view: _ReceiptView, receipt: Mapping[str, object]) -> list[str]:
    """Compare resolution, exact-state, and field observations in receipt order."""
    from rebar._snapshot.ticket_view import PinnedTicketNotFound

    conflicts: list[str] = []
    for ref, raw_expected in _mapping(receipt.get("resolutions")).items():
        resolution_expected = _mapping(raw_expected)
        resolved_ticket = view.resolve(str(ref))
        if resolved_ticket != resolution_expected.get("value"):
            conflicts.append(f"resolution:{ref}")
    for ticket_id, exact_expected in _mapping(receipt.get("exact")).items():
        try:
            exact_actual = _digest(view.show_ticket(str(ticket_id)))
        except PinnedTicketNotFound:
            exact_actual = "<missing>"
        if exact_actual != exact_expected:
            conflicts.append(f"ticket:{ticket_id}")
    for ticket_id, raw_fields in _mapping(receipt.get("fields")).items():
        for field, field_expected in _mapping(raw_fields).items():
            field_actual = view.field_observation(str(ticket_id), str(field))
            if field_actual != field_expected:
                conflicts.append(f"field:{ticket_id}:{field}")
    return conflicts


def _compare_relation_observations(view: _ReceiptView, receipt: Mapping[str, object]) -> list[str]:
    """Compare hierarchy and direct-link observations in receipt order."""
    conflicts: list[str] = []
    for parent, children_expected in _mapping(receipt.get("direct_children")).items():
        children_actual = view.direct_child_ids(str(parent))
        if children_actual != children_expected:
            conflicts.append(f"direct_children:{parent}")
    for parent, descendants_expected in _mapping(receipt.get("descendants")).items():
        descendants_actual = view.transitive_descendant_ids(str(parent))
        if descendants_actual != descendants_expected:
            conflicts.append(f"descendants:{parent}")
    for target, inbound_expected in _mapping(receipt.get("inbound")).items():
        inbound_actual = [list(edge) for edge in view.inbound_links(str(target))]
        if inbound_actual != inbound_expected:
            conflicts.append(f"inbound:{target}")
    for source, outbound_expected in _mapping(receipt.get("outbound")).items():
        state = view.show_ticket(str(source))
        outbound_actual = sorted(
            [str(dep.get("target_id", "")), str(dep.get("relation", ""))]
            for dep in _dependencies(state)
            if dep.get("target_id")
        )
        if outbound_actual != outbound_expected:
            conflicts.append(f"outbound:{source}")
    return conflicts


def _compare_reachability_observations(
    view: _ReceiptView, receipt: Mapping[str, object]
) -> list[str]:
    """Compare transitive relationship predicates in receipt order."""
    conflicts: list[str] = []
    for encoded, expected in _mapping(receipt.get("reachability")).items():
        decoded = json.loads(str(encoded))
        if not isinstance(decoded, list) or len(decoded) != 3:
            raise ValueError("malformed reachability receipt key")
        source, target, raw_relations = decoded
        relations = (
            tuple(str(value) for value in raw_relations) if isinstance(raw_relations, list) else ()
        )
        if view.relation_reachable(str(source), str(target), relations=relations) is not expected:
            conflicts.append(f"reachability:{encoded}")
    return conflicts


def _compare_receipt(view: _ReceiptView, receipt: Mapping[str, object]) -> list[str]:
    return [
        *_compare_ticket_observations(view, receipt),
        *_compare_relation_observations(view, receipt),
        *_compare_reachability_observations(view, receipt),
    ]


def validate_receipt(
    tracker: str,
    receipt: Mapping[str, object],
    *,
    current_oid: TicketsOID | None = None,
) -> ReceiptValidation:
    """Recompute recorded predicates at a descendant revision; unrelated changes pass."""
    if current_oid is not None and not isinstance(current_oid, TicketsOID):
        raise TypeError("current_oid must be a TicketsOID")
    current = current_oid or tracker_head(tracker)
    if not _receipt_schema_matches(receipt):
        return ReceiptValidation(False, current, ("receipt_schema_mismatch",))
    try:
        base = TicketsOID(str(receipt.get("tickets_oid", "")))
    except ValueError:
        return ReceiptValidation(False, current, ("receipt_basis_invalid",))
    ancestry = _run_git(
        tracker,
        "merge-base",
        "--is-ancestor",
        base.value,
        current.value,
        check=False,
    )
    if ancestry.returncode != 0:
        return ReceiptValidation(False, current, ("non_ancestor_tickets_history",))
    from rebar._snapshot.ticket_view import PinnedTicketView

    try:
        with PinnedTicketView.at_oid(tracker, current) as view:
            conflicts = _compare_receipt(view, _thaw_mapping(receipt))
    except Exception as exc:  # noqa: BLE001 — malformed/unreplayable predicates fail closed
        # A demanded predicate that can no longer be replayed is a conflict, not permission
        # to publish against live state and not an untyped traceback at the command boundary.
        conflicts = [f"receipt_replay_error:{type(exc).__name__}"]
    return ReceiptValidation(not conflicts, current, tuple(conflicts))


__all__ = [
    "CodeOID",
    "CompletionReadBasis",
    "ReceiptValidation",
    "TicketsOID",
    "tracker_head",
    "validate_receipt",
]
