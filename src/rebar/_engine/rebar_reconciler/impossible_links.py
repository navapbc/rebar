"""Durable memory of inbound link ADDs that can never succeed (bug b8b1).

The inbound applier used to spend a ``rebar.link`` write on every Jira-sourced
link record every pass, catch whatever came back, log a WARNING and forget it.
Three of those failures are not faults at all — they are deterministic verdicts
from ``rebar.graph._links.add_dependency`` about the shape of the LOCAL ticket
graph:

* the source ticket is ``closed``;
* the two endpoints are already in an ancestor-descendant relationship, so the
  hierarchy expresses the link and a redundant edge is refused;
* the edge would close a cycle.

None of those can change until the local graph changes, so re-attempting them is
pure waste. Measured on four consecutive live passes (runs 31568815075 /
31569179421 / 31569723185 / 31570037358): 19 failures per pass, a byte-identical
set every time.

This module gives the applier the memory it lacked. Two pieces:

``classify`` turns an exception into one of the three permanent reasons, or
``None``. ``None`` is the safety valve — an unrecognised failure is NOT recorded
and keeps the existing retry-next-pass behaviour, so a transient Jira/git/IO
fault is never mistaken for a structural impossibility.

``ImpossibleLinkStore`` persists ``{source|target|relation: record}`` to
``<tracker>/.bridge_state/impossible_links.json``. Each record carries a
**deciding digest** over exactly the structural inputs its validator reads, and
that digest IS the invalidation key: when it stops matching, the record is
pruned and the link is attempted once more on its own merits. Nothing expires on
a timer and nothing needs clearing by hand; deleting the JSON file is still a
complete manual reset.

The input set is per-reason, because the three validators do not read the same
things. ``closed_source`` and ``redundant_ancestry`` are decided entirely by the
two endpoints, so they key on each endpoint's status, parent chain and dep set
and are undisturbed by changes elsewhere. ``cycle`` is NOT: ``add_dependency``
resolves the hierarchy first and then walks the TRANSITIVE dependency graph, so
unlinking or reparenting an intermediate ticket can break the cycle while both
endpoints stay byte-identical. A cycle record therefore keys on the global
structure. Over-invalidating costs one extra attempt; under-invalidating would
suppress a legitimate link indefinitely, so the asymmetry decides the design.

Fail-open throughout: an unreadable store, an uncomputable digest, or an
unreducible ticket all degrade to "attempt the write", never to "suppress it".

Deliberately a SEPARATE file from ``bindings.json``. The binding store's schema
is shared with the peer-state channels, and epic a4bd (never-pushed managed
links vs peer deletions) plans a peer-confirmation record of its own there. A
distinct file keeps the two changes additive.
"""

from __future__ import annotations

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

# Matched against the *rendered* exception text. Each entry is
# ``(reason, (required_substring, ...))`` and ALL substrings must be present —
# two markers rather than one so an unrelated message that happens to contain
# "is closed" cannot be misfiled as a permanent structural verdict.
#
# The exact strings come from rebar/graph/_links.py::add_dependency:
#   "cannot create {relation} link — source ticket '{id}' is closed. ..."
#   "cannot create depends_on link — target ticket '{id}' is closed"
#   "ERROR: redundant link — {a} and {b} are in an ancestor-descendant ..."
#   "Adding {a} → {b} ({rel}) would create a cycle at {level} level"
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
    """Structural fingerprint of one endpoint AND every ancestor on its chain.

    Each link in the chain contributes its own status and dep set, not just its
    id. That is not belt-and-braces: ``add_dependency`` calls
    ``resolve_hierarchy_link`` FIRST, which can REDIRECT an endpoint to a
    type-tier ancestor, and the closed-source / redundant / cycle validators
    then run against that promoted ticket. The deciding status therefore may
    belong to an ancestor rather than to the id the applier passed in.
    Fingerprinting the whole chain covers the redirect target wherever the
    promotion lands, without this module having to re-derive the promotion rule
    and drift from it.

    Anything non-structural about the tickets (title, description, comments,
    tags) is excluded on purpose: a comment must not invalidate a record, or the
    store would churn back to re-attempting on every unrelated edit.
    """
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
    """Fingerprint the WHOLE graph's structure: every ticket's status/parent/deps.

    Needed for the ``cycle`` verdict and only for it. ``check_would_create_cycle``
    walks the TRANSITIVE dependency graph, and it does so on the
    hierarchy-RESOLVED (promoted) endpoints rather than the two ids handed to
    ``rebar.link`` — so unlinking or reparenting some intermediate ticket can
    break the cycle while leaving both endpoints untouched. Keying a cycle
    record on the endpoints alone would therefore under-invalidate and suppress
    a link that has become possible.

    Over-invalidating is the safe direction: a structural change anywhere costs
    one extra attempt, whereas under-invalidating suppresses a legitimate link
    until somebody deletes the file by hand. Comments, titles and descriptions
    are still excluded, so an unchanged *structure* converges to zero writes.
    """
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
        os.makedirs(os.path.dirname(self.path), exist_ok=True)
        payload = {"version": SCHEMA_VERSION, "records": self._records}
        tmp_path = f"{self.path}.tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, indent=2, sort_keys=True)
            os.replace(tmp_path, self.path)
        except OSError as exc:
            logger.warning("impossible_links: could not persist %s (%r)", self.path, exc)
            return
        self._dirty = False
