"""Link event writing and add_dependency for ticket-graph."""

from __future__ import annotations

import glob as _glob
import json
import logging
import os
from collections.abc import Callable

from rebar.reducer._sort import prefix_ts as _prefix_ts

from . import _loader
from ._graph import check_cycle_at_level, check_would_create_cycle
from ._hierarchy import resolve_hierarchy_link
from ._relations import _BLOCKING_RELATIONS
from ._status import _get_ticket_status

logger = logging.getLogger(__name__)

CANONICAL_RELATIONS: frozenset[str] = frozenset(
    # discovered_from: emergent-work provenance (B discovered_from A). Directional
    # (no reciprocal LINK), non-blocking, never cycle-inducing — see _graph.py.
    # caused_by: bug → the change/ticket that caused it. Directional, non-blocking,
    # never cycle-inducing (same semantics as discovered_from).
    {
        "blocks",
        "depends_on",
        "relates_to",
        "duplicates",
        "supersedes",
        "discovered_from",
        "caused_by",
    }
)


class CyclicDependencyError(Exception):
    """Raised when adding a dependency would create a cycle."""

    pass


def _is_active_link(source_id: str, target_id: str, relation: str, tracker_dir: str) -> bool:
    """Return True if a net-active LINK exists from source_id to target_id with the given relation.

    Falls back to scanning SNAPSHOT compiled_state.deps[] when no *-LINK.json files
    are found — ticket-compact.sh bakes LINK events into a SNAPSHOT and deletes the
    original *-LINK.json files (f5a8).
    """
    ticket_dir = os.path.join(tracker_dir, source_id)
    if not os.path.isdir(ticket_dir):
        return False

    _event_order = {"LINK": 0, "UNLINK": 1}
    link_files = [("LINK", f) for f in _glob.glob(os.path.join(ticket_dir, "*-LINK.json"))]
    unlink_files = [("UNLINK", f) for f in _glob.glob(os.path.join(ticket_dir, "*-UNLINK.json"))]
    all_events = sorted(
        link_files + unlink_files,
        key=lambda x: (
            _prefix_ts(x[1]),
            _event_order.get(x[0], 99),
            os.path.basename(x[1]),
        ),
    )

    active_links: dict[str, tuple[str, str]] = {}  # uuid → (target_id, relation)
    # Collect cancelled uuids for the SNAPSHOT fallback below.
    cancelled_uuids: set[str] = set()
    for event_type, filepath in all_events:
        try:
            with open(filepath, encoding="utf-8") as fh:
                ev = json.load(fh)
        except (OSError, json.JSONDecodeError):
            continue
        ev_uuid = ev.get("uuid", "")
        data = ev.get("data", {})
        if event_type == "LINK" and ev_uuid:
            active_links[ev_uuid] = (
                data.get("target_id", data.get("target", "")),
                data.get("relation", ""),
            )
        elif event_type == "UNLINK":
            link_uuid = data.get("link_uuid", "")
            if link_uuid:
                cancelled_uuids.add(link_uuid)
                active_links.pop(link_uuid, None)

    if any(tid == target_id and rel == relation for tid, rel in active_links.values()):
        return True

    # Compaction moves LINKs into SNAPSHOT deps and deletes their files. If no
    # active LINK remains, accept a matching snapshot unless a later UNLINK
    # cancelled it.
    for snap_path in sorted(_glob.glob(os.path.join(ticket_dir, "*-SNAPSHOT.json"))):
        try:
            with open(snap_path, encoding="utf-8") as fh:
                snap = json.load(fh)
        except (OSError, json.JSONDecodeError):
            continue
        compiled = snap.get("data", {}).get("compiled_state", {})
        for dep in compiled.get("deps", []):
            dep_target = dep.get("target_id", "")
            dep_uuid = dep.get("link_uuid", "")
            dep_relation = dep.get("relation", "")
            if (
                dep_target == target_id
                and dep_relation == relation
                and dep_uuid
                and dep_uuid not in cancelled_uuids
            ):
                return True

    return False


def _write_link_event(
    source_id: str,
    target_id: str,
    relation: str,
    tracker_dir: str,
    provenance: str | None = None,
) -> None:
    """Append one LINK to ``source_id`` through the canonical locked write seam.

    This helper performs neither cycle nor idempotency checks. The shared path
    owns envelope and HLC construction, dual locking, atomic rename, rebase
    protection, commit, and best-effort push. Commit failures raise
    :class:`rebar._commands._seam.CommandError`. Push failures do not. Optional
    ``provenance`` records caused-by attribution without changing unmarked payloads.
    """
    from pathlib import Path

    from rebar._commands import _seam

    data: dict = {"target_id": target_id, "relation": relation}
    if provenance:
        data["provenance"] = provenance
    _seam.append_event(
        source_id,
        "LINK",
        data,
        Path(tracker_dir),
    )


def _resolve_link_endpoints(
    source_id: str, target_id: str, tracker_dir: str, relation: str
) -> tuple[str, str, dict | None]:
    """Validate the relation, resolve hierarchy promotion, compose the REDIRECT record.

    The validation + promotion prologue of :func:`add_dependency`, extracted along
    its existing seam so the caller stays under the complexity ceiling. Returns
    ``(resolved_source, resolved_target, redirect_record)`` where the record is
    ``None`` unless promotion moved an endpoint. Raises ValueError on an invalid
    relation, a resolver error, or a redundant (ancestor-descendant) link.
    """
    if relation not in CANONICAL_RELATIONS:
        canonical_list = ", ".join(sorted(CANONICAL_RELATIONS))
        raise ValueError(f"invalid relation '{relation}': must be one of {canonical_list}")

    # The relation is passed through so the resolver can gate promotion: only
    # blocking deps (blocks/depends_on) are promoted to a comparable type-tier;
    # all other relations link the exact pair.
    hierarchy_result = resolve_hierarchy_link(source_id, target_id, tracker_dir, relation)

    if "error" in hierarchy_result:
        raise ValueError(hierarchy_result["error"])

    if hierarchy_result.get("is_redundant"):
        msg = (
            f"ERROR: redundant link — {source_id} and {target_id} are in an "
            "ancestor-descendant relationship; the hierarchy already expresses it"
        )
        logger.error(msg)
        raise ValueError(msg)

    resolved_source = str(hierarchy_result["resolved_source"])
    resolved_target = str(hierarchy_result["resolved_target"])

    redirect_record = None
    if hierarchy_result.get("was_redirected"):
        logger.warning(
            "REDIRECT: %s\u2192%s promoted to %s\u2192%s",
            source_id,
            target_id,
            resolved_source,
            resolved_target,
        )
        redirect_record = {
            "redirected": True,
            "original": {"source": source_id, "target": target_id},
            "resolved": {"source": resolved_source, "target": resolved_target},
        }
    return resolved_source, resolved_target, redirect_record


def add_dependency(
    source_id: str,
    target_id: str,
    tracker_dir: str,
    relation: str = "blocks",
    *,
    on_outcome: Callable[[dict], None] | None = None,
) -> dict | None:
    """Add a validated, cycle-safe dependency idempotently.

    The source stores the LINK. ``relates_to`` also writes its reciprocal.
    Hierarchy promotion returns a REDIRECT record, printed only by CLI callers
    after persistence. ``on_outcome`` receives exactly one resolved wrote/no-op
    record. Invalid relations and cycles raise their documented errors.
    """
    # Resolve grammar and hierarchy promotion now, but emit a REDIRECT only after
    # the LINK is durable so a broken stdout pipe cannot lose the write.
    resolved_source, resolved_target, redirect_record = _resolve_link_endpoints(
        source_id, target_id, tracker_dir, relation
    )

    def _emit_redirect() -> None:
        if redirect_record is not None:
            print(  # noqa: T201 \u2014 stdout data: machine-readable redirect record (CLI contract)
                json.dumps(redirect_record)
            )

    source_id = resolved_source
    target_id = resolved_target

    if check_would_create_cycle(source_id, target_id, relation, tracker_dir):
        raise CyclicDependencyError(
            f"Adding {resolved_source} → {resolved_target} ({relation}) would create a cycle"
        )

    resolved_source_dir = os.path.join(tracker_dir, resolved_source)
    resolved_source_state = (
        _loader.reduce_ticket(resolved_source_dir) if os.path.isdir(resolved_source_dir) else None
    )
    level = (
        (resolved_source_state.get("ticket_type") or "").lower() if resolved_source_state else ""
    )
    # Only the cycle-capable relations are subject to the per-level cycle guard; every
    # other relation is non-blocking and never cycle-inducing (mirrors
    # check_would_create_cycle). The non-blocking set is deliberately not listed here —
    # this comment used to name four of the five, having missed caused_by (mirror F4).
    if (
        relation in _BLOCKING_RELATIONS
        and level
        and check_cycle_at_level(resolved_source, resolved_target, level, tracker_dir)
    ):
        if resolved_source == resolved_target:
            raise CyclicDependencyError(
                f"Adding {resolved_source} → {resolved_target} ({relation}) "
                f"is a self-referential dependency at {level} level"
            )
        raise CyclicDependencyError(
            f"Adding {resolved_source} → {resolved_target} ({relation}) "
            f"would create a cycle at {level} level"
        )

    source_status = _get_ticket_status(source_id, tracker_dir)
    if source_status == "closed":
        raise ValueError(
            f"cannot create {relation} link — source ticket '{source_id}' is closed. "
            f"Reopen it first with: ticket transition {source_id} closed open"
        )

    if relation == "depends_on":
        target_status = _get_ticket_status(target_id, tracker_dir)
        if target_status == "closed":
            raise ValueError(
                f"cannot create depends_on link — target ticket '{target_id}' is closed"
            )

    def _report_outcome(wrote: bool) -> None:
        if on_outcome is not None:
            on_outcome(
                {"wrote": wrote, "source": source_id, "target": target_id, "relation": relation}
            )

    if _is_active_link(source_id, target_id, relation, tracker_dir):
        # Idempotent no-op: the link already exists. Nothing durable to protect, so
        # surface the redirect record (parity with the pre-fix behavior) and return.
        _emit_redirect()
        _report_outcome(False)
        return redirect_record

    # caused_by via `rebar link` is an attribution SUPPLIED by the caller (CLI, library,
    # or MCP), never a blame guess — mark it so the escaped-defect lenses can weight
    # proven attributions above derived ones (ticket 6536-367c).
    _write_link_event(
        source_id,
        target_id,
        relation,
        tracker_dir,
        provenance="explicit" if relation == "caused_by" else None,
    )

    if relation == "relates_to" and not _is_active_link(
        target_id, source_id, relation, tracker_dir
    ):
        _write_link_event(target_id, source_id, relation, tracker_dir)

    # Durable write(s) committed — now it is safe to emit the redirect record to
    # stdout. A BrokenPipeError here propagates loudly (exit non-zero) but the link
    # is already persisted, satisfying the write-or-fail-loudly invariant.
    _emit_redirect()
    _report_outcome(True)
    return redirect_record


def remove_dependency(
    source_id: str,
    target_id: str,
    tracker_dir: str,
    relation: str,
) -> None:
    """Remove one active ``(target_id, relation)`` through the shared lock.

    Other relations between the pair remain. ``relates_to`` removes its reciprocal.
    Invalid relations raise ``ValueError``. Missing tickets or links raise
    :class:`rebar._commands._seam.CommandError`. CLI and library callers share the
    canonical UNLINK replay in ``rebar._commands.unlink``.
    """
    if relation not in CANONICAL_RELATIONS:
        canonical_list = ", ".join(sorted(CANONICAL_RELATIONS))
        raise ValueError(f"invalid relation '{relation}': must be one of {canonical_list}")

    from pathlib import Path

    from rebar._commands.unlink import _get_link_info, _write_unlink

    tracker = Path(tracker_dir)
    _write_unlink(source_id, target_id, tracker, repo_root=None, relation=relation)

    if relation == "relates_to":
        recip_uuid, _ = _get_link_info(tracker / target_id, source_id, relation)
        if recip_uuid:
            _write_unlink(target_id, source_id, tracker, repo_root=None, relation=relation)
        else:
            logger.warning(
                "no reciprocal LINK found in '%s' targeting '%s' — orphaned link, "
                "removed from '%s' only",
                target_id,
                source_id,
                source_id,
            )
