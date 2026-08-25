"""Lazy, immutable ticket reads from one committed tracker revision.

The code snapshot and the ticket store are independent histories.  This module gives
completion verification a typed ticket-store handle that reads Git objects directly,
materializes only tickets actually reduced, and records every cross-ticket predicate in
a receipt that can be revalidated before close publication.
"""

from __future__ import annotations

import copy
import json
import shutil
import tempfile
import time
import uuid
from collections import deque
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING

from rebar._snapshot.ticket_objects import TicketObjectStore
from rebar._snapshot.ticket_receipt import (
    CodeOID,
    CompletionReadBasis,
    ReceiptValidation,
    TicketsOID,
    _digest,
    tracker_head,
    validate_receipt,
)

if TYPE_CHECKING:
    from rebar._engine_support.ticket_query import TicketQuery


def _dependencies(state: Mapping[str, object]) -> tuple[Mapping[str, object], ...]:
    """Narrow the reducer's dynamic dependency payload at the snapshot boundary."""
    raw = state.get("deps")
    if not isinstance(raw, list):
        return ()
    return tuple(dep for dep in raw if isinstance(dep, Mapping))


class PinnedTicketViewError(RuntimeError):
    """Base error for an immutable ticket view."""


class PinnedTicketNotFound(PinnedTicketViewError):
    """The requested reference did not resolve at the pinned tracker revision."""


class UnsupportedPinnedQuery(PinnedTicketViewError):
    """A broad query cannot be answered without violating the lazy-read contract."""


class PinnedTicketView:
    """Demand-reduced ticket state at one immutable tracker commit."""

    def __init__(self, tracker: str, tickets_oid: TicketsOID, *, run_id: str | None = None):
        if not isinstance(tickets_oid, TicketsOID):
            raise TypeError("PinnedTicketView requires a TicketsOID")
        self.tracker = str(Path(tracker).resolve())
        self.tickets_oid = tickets_oid
        self.run_id = run_id or str(uuid.uuid4())
        self.metrics: dict[str, int] = {
            "ticket_object_list_ms": 0,
            "ticket_object_read_ms": 0,
            "ticket_reduction_ms": 0,
            "ticket_object_reads": 0,
        }
        self._tmp = tempfile.mkdtemp(prefix=f"rebar-ticket-view-{self.run_id[:8]}-")
        self._temp_tracker = Path(self._tmp) / ".tickets-tracker"
        self._temp_tracker.mkdir()
        self._objects = TicketObjectStore(self.tracker, self.tickets_oid, self.metrics)
        self._states: dict[str, dict[str, object]] = {}
        self._receipt_exact: dict[str, str] = {}
        self._receipt_fields: dict[str, dict[str, dict[str, object]]] = {}
        self._receipt_resolution: dict[str, dict[str, str | None]] = {}
        self._receipt_direct: dict[str, tuple[str, ...]] = {}
        self._receipt_descendants: dict[str, tuple[str, ...]] = {}
        self._receipt_inbound: dict[str, tuple[tuple[str, str, str], ...]] = {}
        self._receipt_outbound: dict[str, tuple[tuple[str, str], ...]] = {}
        self._receipt_reachability: dict[str, bool] = {}
        self._closed = False

    @classmethod
    def try_capture(
        cls, repo_root: str | None, *, fetch: bool, run_id: str | None = None
    ) -> PinnedTicketView | None:
        """Pin the live tracker, returning ``None`` when no object source is available."""
        from rebar import config
        from rebar._snapshot import repo_snapshot

        root = str(config.repo_root(repo_root))
        try:
            branch = config.tickets_branch(root)
            remote = config.tickets_remote(root)
        except config.ConfigError:
            return None
        started = time.monotonic_ns()
        pinned = repo_snapshot._pin_tickets_sha(root, branch, remote, fetch=fetch)
        if pinned is None:
            return None
        oid, source = pinned
        view = cls(source, TicketsOID(oid), run_id=run_id)
        view.metrics["tickets_oid_capture_ms"] = (time.monotonic_ns() - started) // 1_000_000
        return view

    @classmethod
    def at_oid(
        cls, tracker: str, tickets_oid: TicketsOID, *, run_id: str | None = None
    ) -> PinnedTicketView:
        return cls(tracker, tickets_oid, run_id=run_id)

    def close(self) -> None:
        if not self._closed:
            shutil.rmtree(self._tmp, ignore_errors=True)
            self._closed = True

    def __enter__(self) -> PinnedTicketView:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    def _assert_open(self) -> None:
        if self._closed:
            raise PinnedTicketViewError("pinned ticket view is closed")

    def resolve(self, ticket_ref: str) -> str | None:
        ref = str(ticket_ref)
        self._assert_open()
        resolved, kind = self._objects.resolve(ref)
        self._receipt_resolution[ref] = {"kind": kind, "value": resolved}
        return resolved

    def _materialize_resolver_support(self, raw: str) -> None:
        directories, blobs = self._objects.resolver_material(raw)
        for ticket_id in directories:
            (self._temp_tracker / ticket_id).mkdir(exist_ok=True)
        for path, blob in blobs.items():
            target = self._temp_tracker / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(blob)

    def _load_state(self, canonical: str) -> dict[str, object]:
        cached = self._states.get(canonical)
        if cached is not None:
            return cached
        paths = list(self._objects.ticket_event_paths(canonical))
        blobs = self._objects.cat_files(paths)
        ticket_dir = self._temp_tracker / canonical
        ticket_dir.mkdir(exist_ok=True)
        for path, blob in blobs.items():
            target = self._temp_tracker / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(blob)
        for path, blob in blobs.items():
            try:
                event = json.loads(blob)
            except (TypeError, ValueError) as exc:
                raise PinnedTicketViewError(
                    f"corrupt JSON in pinned ticket object {path!r}: {exc}"
                ) from None
            if not isinstance(event, dict):
                raise PinnedTicketViewError(f"pinned ticket object {path!r} is not a JSON object")
            if str(event.get("event_type", "")).upper() != "LINK":
                continue
            data = event.get("data", {}) or {}
            if not isinstance(data, dict):
                raise PinnedTicketViewError(
                    f"pinned LINK object {path!r} has non-object event data"
                )
            raw = str(data.get("target_id", data.get("target", "")) or "")
            if raw:
                # Reducer support for an internally scanned ticket is not itself an observed
                # reference-resolution predicate. A demanded full ticket is protected by its
                # exact reduced-state digest; graph-only scans record only their returned
                # predicate below. Avoiding ``self.resolve`` here prevents a grep false-positive
                # from making an unrelated alias or Jira binding part of the receipt.
                self._materialize_resolver_support(raw)
        from rebar.reducer import reduce_ticket
        from rebar.reducer._present import public_state

        started = time.monotonic_ns()
        event_paths = [str(self._temp_tracker / path) for path in sorted(blobs)]
        state = reduce_ticket(str(ticket_dir), event_files_override=event_paths)
        self.metrics["ticket_reduction_ms"] += (time.monotonic_ns() - started) // 1_000_000
        if not isinstance(state, dict) or not state.get("ticket_type"):
            raise PinnedTicketNotFound(f"ticket {canonical!r} has no reducible state")
        public: dict[str, object] = public_state(state)
        self._states[canonical] = public
        return public

    def _read_canonical(self, canonical: str, *, record_exact: bool) -> dict[str, object]:
        """Copy one reduced state and optionally record the full observable ticket value."""
        state = copy.deepcopy(self._load_state(canonical))
        if record_exact:
            self._receipt_exact[canonical] = _digest(state)
            self._receipt_outbound[canonical] = tuple(
                sorted(
                    (str(dep.get("target_id", "")), str(dep.get("relation", "")))
                    for dep in _dependencies(state)
                    if dep.get("target_id")
                )
            )
        return state

    def show_ticket(self, ticket_ref: str, *, include_inbound: bool = False) -> dict[str, object]:
        canonical = self.resolve(ticket_ref)
        if canonical is None:
            raise PinnedTicketNotFound(
                f"ticket {ticket_ref!r} was not found at tickets OID {self.tickets_oid.value}"
            )
        state = self._read_canonical(canonical, record_exact=True)
        if include_inbound:
            state["inbound_deps"] = [
                {"from_id": source, "relation": relation, "status": status}
                for source, relation, status in self.inbound_links(canonical)
            ]
        return state

    def field_value(self, ticket_ref: str, field: str) -> object:
        """Return one pinned public field and record only that field-level predicate."""
        canonical = self.resolve(ticket_ref)
        if canonical is None:
            raise PinnedTicketNotFound(f"ticket {ticket_ref!r} was not found")
        state = self._read_canonical(canonical, record_exact=False)
        value = copy.deepcopy(state.get(field))
        self._receipt_fields.setdefault(canonical, {})[str(field)] = {
            "present": field in state,
            "digest": _digest(value),
        }
        return value

    def field_observation(self, ticket_ref: str, field: str) -> dict[str, object]:
        """Return the receipt predicate produced by :meth:`field_value`."""
        canonical = self.resolve(ticket_ref)
        if canonical is None:
            raise PinnedTicketNotFound(f"ticket {ticket_ref!r} was not found")
        self.field_value(canonical, field)
        return copy.deepcopy(self._receipt_fields[canonical][str(field)])

    def _grep_ticket_ids(self, needle: str) -> tuple[str, ...]:
        self._assert_open()
        return self._objects.grep_ticket_ids(needle)

    def direct_child_ids(self, ticket_ref: str) -> list[str]:
        """Return direct membership without promoting child fields to exact dependencies."""
        canonical = self.resolve(ticket_ref)
        if canonical is None:
            raise PinnedTicketNotFound(f"ticket {ticket_ref!r} was not found")
        child_ids: list[str] = []
        for candidate in self._grep_ticket_ids(canonical):
            state = self._read_canonical(candidate, record_exact=False)
            if state.get("parent_id") == canonical:
                child_ids.append(candidate)
        child_ids.sort()
        self._receipt_direct[canonical] = tuple(child_ids)
        return child_ids

    def direct_children(
        self, ticket_ref: str, *, include_archived: bool = False
    ) -> list[dict[str, object]]:
        all_children = [
            self._read_canonical(ticket_id, record_exact=True)
            for ticket_id in self.direct_child_ids(ticket_ref)
        ]
        if include_archived:
            return all_children
        return [item for item in all_children if not item.get("archived")]

    def transitive_descendant_ids(self, ticket_ref: str) -> list[str]:
        """Return breadth-first descendant membership without demanding full child states."""
        canonical = self.resolve(ticket_ref)
        if canonical is None:
            raise PinnedTicketNotFound(f"ticket {ticket_ref!r} was not found")
        found: list[str] = []
        seen = {canonical}
        frontier = deque([canonical])
        while frontier:
            for cid in self.direct_child_ids(frontier.popleft()):
                if cid and cid not in seen:
                    seen.add(cid)
                    frontier.append(cid)
                    found.append(cid)
        self._receipt_descendants[canonical] = tuple(found)
        return found

    def transitive_descendants(self, ticket_ref: str) -> list[dict[str, object]]:
        return [
            self._read_canonical(ticket_id, record_exact=True)
            for ticket_id in self.transitive_descendant_ids(ticket_ref)
        ]

    def inbound_links(self, ticket_ref: str) -> list[tuple[str, str, str]]:
        canonical = self.resolve(ticket_ref)
        if canonical is None:
            raise PinnedTicketNotFound(f"ticket {ticket_ref!r} was not found")
        links: list[tuple[str, str, str]] = []
        for candidate in self._objects.inbound_candidate_ids(canonical):
            if candidate == canonical:
                continue
            state = self._read_canonical(candidate, record_exact=False)
            for dep in _dependencies(state):
                if dep.get("target_id") == canonical:
                    links.append(
                        (candidate, str(dep.get("relation", "")), str(state.get("status", "")))
                    )
        links.sort()
        self._receipt_inbound[canonical] = tuple(links)
        return links

    def relation_reachable(
        self, source_ref: str, target_ref: str, *, relations: Iterable[str]
    ) -> bool:
        source = self.resolve(source_ref)
        target = self.resolve(target_ref)
        relation_set = frozenset(str(value) for value in relations)
        if source is None or target is None:
            reachable = False
        else:
            frontier = deque([source])
            seen = {source}
            reachable = source == target
            while frontier and not reachable:
                current = frontier.popleft()
                state = self._read_canonical(current, record_exact=False)
                for dep in _dependencies(state):
                    nxt = str(dep.get("target_id", ""))
                    if dep.get("relation") not in relation_set or not nxt or nxt in seen:
                        continue
                    if nxt == target:
                        reachable = True
                        break
                    seen.add(nxt)
                    frontier.append(nxt)
        key = json.dumps([source_ref, target_ref, sorted(relation_set)], separators=(",", ":"))
        self._receipt_reachability[key] = reachable
        return reachable

    def list_by_query(self, query: TicketQuery) -> list[dict[str, object]]:
        """Answer the bounded parent query used by completion; reject broad scans."""
        if not query.parent:
            raise UnsupportedPinnedQuery(
                "pinned completion views require a parent-bounded ticket query"
            )
        if query.min_children is not None or query.blocking_state:
            raise UnsupportedPinnedQuery("aggregate/blocking ticket queries are unsupported")
        states = self.direct_children(query.parent, include_archived=query.include_archived)
        ticket_type = query.ticket_type
        has_tag = query.has_tag
        if has_tag.startswith("detected_by:") and not ticket_type:
            ticket_type = "bug"
        from rebar.reducer._api import _NON_GRAPH_ARTIFACT_TYPES
        from rebar.reducer._filters import apply_ticket_filters

        requested_types = {value.strip() for value in ticket_type.split(",") if value.strip()}
        if requested_types.isdisjoint(_NON_GRAPH_ARTIFACT_TYPES):
            states = [
                state
                for state in states
                if state.get("ticket_type") not in _NON_GRAPH_ARTIFACT_TYPES
            ]
        states = apply_ticket_filters(
            states,
            type_filter=ticket_type,
            status_filter=query.status,
            tag_filter=has_tag,
            priority_filter=query.priority,
            without_tag_filter=query.without_tag,
        )
        if query.exclude_deleted:
            states = [state for state in states if state.get("status") != "deleted"]
        out: list[dict[str, object]] = []
        for state in states:
            item = copy.deepcopy(state)
            if not query.include_body:
                item.pop("description", None)
                item.pop("comments", None)
            if query.with_children_count:
                item["children_count"] = len(self.direct_children(str(item["ticket_id"])))
            out.append(item)
        from rebar._engine_support.reads import sort_states

        return sort_states(out, query.sort)

    def event_payloads(self, ticket_ref: str, event_type: str) -> list[dict[str, object]]:
        """Return pinned raw payloads for one event type, in event order."""
        canonical = self.resolve(ticket_ref)
        if canonical is None:
            raise PinnedTicketNotFound(f"ticket {ticket_ref!r} was not found")
        return self._objects.event_payloads(canonical, event_type)

    def receipt(self) -> dict[str, object]:
        from rebar.reducer import SCHEMA_VERSION

        receipt: dict[str, object] = {
            "schema": "ticket_read_receipt_v1",
            "view_schema_version": 1,
            "reducer_schema_version": SCHEMA_VERSION,
            "tickets_oid": self.tickets_oid.value,
            "exact": dict(sorted(self._receipt_exact.items())),
            "fields": {
                ticket_id: dict(sorted(fields.items()))
                for ticket_id, fields in sorted(self._receipt_fields.items())
            },
            "resolutions": dict(sorted(self._receipt_resolution.items())),
            "negative": sorted(
                key for key, value in self._receipt_resolution.items() if value["value"] is None
            ),
            "direct_children": {
                key: list(value) for key, value in sorted(self._receipt_direct.items())
            },
            "descendants": {
                key: list(value) for key, value in sorted(self._receipt_descendants.items())
            },
            "inbound": {
                key: [list(edge) for edge in value]
                for key, value in sorted(self._receipt_inbound.items())
            },
            "outbound": {
                key: [list(edge) for edge in value]
                for key, value in sorted(self._receipt_outbound.items())
            },
            "reachability": dict(sorted(self._receipt_reachability.items())),
        }
        return receipt

    def completion_basis(self, code_oid: CodeOID) -> CompletionReadBasis:
        if not isinstance(code_oid, CodeOID):
            raise TypeError("completion_basis requires a CodeOID")
        receipt = self.receipt()
        return CompletionReadBasis(
            run_id=self.run_id,
            code_oid=code_oid,
            tickets_oid=self.tickets_oid,
            receipt=receipt,
            receipt_digest=_digest(receipt),
        )


__all__ = [
    "CodeOID",
    "CompletionReadBasis",
    "PinnedTicketNotFound",
    "PinnedTicketView",
    "PinnedTicketViewError",
    "ReceiptValidation",
    "TicketsOID",
    "UnsupportedPinnedQuery",
    "tracker_head",
    "validate_receipt",
]
