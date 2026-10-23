"""Remember inbound links whose local graph cannot accept them yet.

Classify only closed-source, redundant-ancestry, and cycle errors as structural.
Unknown errors remain retryable. ImpossibleLinkStore keys each record by source,
target, and relation with a deciding structural digest. Endpoint chains suffice
for closed or redundant links. Cycles need the whole dependency graph. A changed
digest retries the link. Unreadable state or an unavailable digest also retries.
The separate file avoids changing binding-store schema."""

from __future__ import annotations

import copy
import hashlib
import json
import logging
import os
import time
from typing import Any

logger = logging.getLogger(__name__)

SCHEMA_VERSION = 1

#: Path of the store relative to the tracker dir.
STORE_RELATIVE = os.path.join(".bridge_state", "impossible_links.json")

REASON_CLOSED_SOURCE = "closed_source"
REASON_REDUNDANT_ANCESTRY = "redundant_ancestry"
REASON_CYCLE = "cycle"

PERMANENT_REASONS: frozenset[str] = frozenset(
    {REASON_CLOSED_SOURCE, REASON_REDUNDANT_ANCESTRY, REASON_CYCLE}
)

# Require every substring of a known graph error before recording a permanent
# structural reason. A partial match must remain retryable.
_SIGNATURES: tuple[tuple[str, tuple[str, ...]], ...] = (
    (REASON_REDUNDANT_ANCESTRY, ("redundant link", "ancestor-descendant")),
    (REASON_CYCLE, ("would create a cycle",)),
    (REASON_CLOSED_SOURCE, ("cannot create", "is closed")),
)

# Guard the parent walk: a malformed store must not spin here.
_MAX_ANCESTRY_DEPTH = 64


def classify(exc: BaseException | str) -> str | None:
    """Return the permanent reason behind ``exc``, or ``None`` if not permanent.

    ``None`` means "this failure is not provably structural" and MUST leave the
    caller's existing behaviour untouched: attempt again next pass. Only the
    three verdicts in :data:`PERMANENT_REASONS` are decidable from local state
    and therefore safe to remember.
    """
    text = exc if isinstance(exc, str) else f"{exc!r}"
    for reason, markers in _SIGNATURES:
        if all(marker in text for marker in markers):
            return reason
    return None


def _endpoint_fingerprint(ticket_id: str, tracker_dir: str) -> str:
    """Hash each endpoint and ancestor status, parent, and dependency set. Hierarchy
    promotion may move the deciding status to an ancestor. Exclude nonstructural
    prose and tags so unrelated edits do not reattempt a link."""
    from rebar.graph._loader import reduce_ticket
    from rebar.graph._status import _get_ticket_status

    chain: list[str] = []
    seen: set[str] = set()
    current: str | None = ticket_id
    for _ in range(_MAX_ANCESTRY_DEPTH):
        if not current or current in seen:
            break
        seen.add(current)
        status = _get_ticket_status(current, tracker_dir)
        state: dict[str, Any] | None
        try:
            state = reduce_ticket(os.path.join(tracker_dir, current))
        except Exception:  # noqa: BLE001 — an unreducible ancestor contributes no structure
            state = None
        deps = (
            sorted(
                f"{dep.get('relation')}->{dep.get('target_id')}"
                for dep in (state.get("deps") or [])
                if isinstance(dep, dict) and dep.get("relation") and dep.get("target_id")
            )
            if isinstance(state, dict)
            else []
        )
        chain.append(f"{current}[status={status};deps={','.join(deps)}]")
        if not isinstance(state, dict):
            break
        parent = state.get("parent_id")
        current = str(parent) if parent else None

    return ">".join(chain)


def _global_structure_fingerprint(tracker_dir: str) -> str:
    """Hash graph status, parent, and dependencies for cycle verdicts. A change to an
    intermediate ticket can break a cycle without changing either endpoint. Extra
    retries after structural changes are safer than suppressing a valid link."""
    from rebar.reducer import reduce_all_tickets

    rows: list[str] = []
    for state in reduce_all_tickets(tracker_dir):
        if not isinstance(state, dict):
            continue
        ticket_id = state.get("ticket_id")
        if not ticket_id:
            continue
        deps = sorted(
            f"{dep.get('relation')}->{dep.get('target_id')}"
            for dep in (state.get("deps") or [])
            if isinstance(dep, dict) and dep.get("relation") and dep.get("target_id")
        )
        rows.append(
            f"{ticket_id}:{state.get('status')}:{state.get('parent_id') or ''}:{','.join(deps)}"
        )
    rows.sort()
    return hashlib.sha256("\n".join(rows).encode("utf-8")).hexdigest()


def deciding_digest(
    source_id: str,
    target_id: str,
    relation: str,
    tracker_dir: str,
    reason: str,
    graph_cache: dict[str, str] | None = None,
) -> str:
    """Hash the structural inputs that decide whether this link can be created.

    A record whose stored digest still equals this value describes a world that
    has not moved, so the verdict still holds and the write can be skipped. A
    changed digest re-qualifies the link for exactly one fresh attempt.

    The input SET depends on the reason, because the three validators do not
    read the same things. ``closed_source`` and ``redundant_ancestry`` are
    decided entirely by the two endpoints (a status, and the ancestor chains),
    so those digests stay narrow and a change elsewhere in the store does not
    disturb them. ``cycle`` is decided by a transitive walk over the whole
    dependency graph, so it keys on the global structure — see
    :func:`_global_structure_fingerprint`.
    """
    parts = [
        f"v{SCHEMA_VERSION}",
        reason,
        relation,
        _endpoint_fingerprint(source_id, tracker_dir),
        _endpoint_fingerprint(target_id, tracker_dir),
    ]
    if reason == REASON_CYCLE:
        # Memoised per pass: the global projection reduces every ticket, so
        # recomputing it once per cycle record would make the cost quadratic in
        # a store with several of them.
        if graph_cache is None:
            graph = _global_structure_fingerprint(tracker_dir)
        else:
            graph = graph_cache.get(tracker_dir) or ""
            if not graph:
                graph = _global_structure_fingerprint(tracker_dir)
                graph_cache[tracker_dir] = graph
        parts.append("graph=" + graph)
    return hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()


def record_key(source_id: str, target_id: str, relation: str) -> str:
    """The store key for one ordered, relation-qualified link triple."""
    return f"{source_id}|{target_id}|{relation}"


class ImpossibleLinkStore:
    """Read/modify/write access to the durable impossible-link records.

    Loads eagerly and writes only when something actually changed, so a
    converged pass touches the file zero times. A corrupt or unreadable store
    degrades to empty rather than raising: losing the memory costs one wasted
    retry, whereas raising would break the inbound pass.
    """

    def __init__(self, tracker_dir: str) -> None:
        self.tracker_dir = str(tracker_dir)
        self.path = os.path.join(self.tracker_dir, STORE_RELATIVE)
        self._records: dict[str, dict[str, Any]] = {}
        self._baseline_records: dict[str, dict[str, Any]] = {}
        self._dirty = False
        # One pass = one store instance = one global-projection computation.
        self._graph_cache: dict[str, str] = {}
        self._load()

    def _load(self) -> None:
        try:
            with open(self.path, encoding="utf-8") as handle:
                data = json.load(handle)
        except FileNotFoundError:
            return
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(
                "impossible_links: ignoring unreadable store %s (%r); "
                "impossible links will be re-attempted once",
                self.path,
                exc,
            )
            return
        if not isinstance(data, dict):
            return
        records = data.get("records")
        if isinstance(records, dict):
            self._records = {
                key: dict(value) for key, value in records.items() if isinstance(value, dict)
            }
        self._baseline_records = copy.deepcopy(self._records)

    def __len__(self) -> int:
        return len(self._records)

    def get(self, source_id: str, target_id: str, relation: str) -> dict[str, Any] | None:
        """The stored record for a triple, or ``None``."""
        return self._records.get(record_key(source_id, target_id, relation))

    def should_skip(self, source_id: str, target_id: str, relation: str) -> str | None:
        """Return the recorded reason when this link is still known-impossible.

        ``None`` means "attempt it": either nothing is recorded, or the deciding
        inputs moved since the record was written, which re-qualifies the link.
        A record is refreshed in place when it is used, so ``last_seen`` and
        ``attempts`` stay meaningful for an operator reading the file.
        """
        record = self.get(source_id, target_id, relation)
        if record is None:
            return None
        reason = record.get("reason")
        if reason not in PERMANENT_REASONS:
            return None
        try:
            current = deciding_digest(source_id, target_id, relation, self.tracker_dir, str(reason))
        except Exception:  # noqa: BLE001 — an undecidable digest must not suppress the write
            return None
        if current != record.get("digest"):
            # The world moved. Drop the record rather than leaving it to be
            # overwritten only if the retry fails again — otherwise a link that
            # became possible leaves a dead entry in the file forever, and the
            # store grows without bound.
            del self._records[record_key(source_id, target_id, relation)]
            self._dirty = True
            return None
        record["last_seen"] = time.time()
        record["skips"] = int(record.get("skips") or 0) + 1
        self._dirty = True
        return str(reason)

    def record(self, source_id: str, target_id: str, relation: str, reason: str) -> bool:
        """Remember that this link is permanently impossible for the reason given.

        Returns True when the record is NEW (or its reason/digest changed) —
        the caller uses that to decide whether the operator has already been
        told. Returns False for a non-permanent reason, which is never stored.
        """
        if reason not in PERMANENT_REASONS:
            return False
        try:
            digest = deciding_digest(
                source_id, target_id, relation, self.tracker_dir, reason, self._graph_cache
            )
        except Exception as exc:  # noqa: BLE001 — no digest, no record: retry next pass
            logger.debug(
                "impossible_links: cannot digest %s -> %s (%s): %r",
                source_id,
                target_id,
                relation,
                exc,
            )
            return False
        key = record_key(source_id, target_id, relation)
        now = time.time()
        previous = self._records.get(key)
        is_new = (
            previous is None or previous.get("digest") != digest or previous.get("reason") != reason
        )
        self._records[key] = {
            "source_id": source_id,
            "target_id": target_id,
            "relation": relation,
            "reason": reason,
            "digest": digest,
            "first_seen": (previous or {}).get("first_seen", now) if not is_new else now,
            "last_seen": now,
            "attempts": int((previous or {}).get("attempts") or 0) + 1,
            "skips": 0 if is_new else int((previous or {}).get("skips") or 0),
        }
        self._dirty = True
        return is_new

    def save(self) -> None:
        """Persist the records if anything changed (atomic replace)."""
        if not self._dirty:
            return
        from rebar.reconciler.sidecar_transactions import persist_record_deltas

        if persist_record_deltas(
            self.path,
            version=SCHEMA_VERSION,
            baseline_records=self._baseline_records,
            desired_records=self._records,
            log_label="impossible_links",
        ):
            self._baseline_records = copy.deepcopy(self._records)
            self._dirty = False
