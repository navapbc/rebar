"""Link event writing and add_dependency for ticket-graph."""

from __future__ import annotations

import glob as _glob
import json
import os
import sys

from rebar.reducer._sort import prefix_ts as _prefix_ts

from ._graph import check_cycle_at_level, check_would_create_cycle
from ._hierarchy import resolve_hierarchy_link
from ._loader import reduce_ticket
from ._status import _get_ticket_status

CANONICAL_RELATIONS: frozenset[str] = frozenset(
    # discovered_from: emergent-work provenance (B discovered_from A). Directional
    # (no reciprocal LINK), non-blocking, never cycle-inducing — see _graph.py.
    {"blocks", "depends_on", "relates_to", "duplicates", "supersedes", "discovered_from"}
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

    # ── SNAPSHOT fallback (f5a8) ──────────────────────────────────────────────
    # ticket-compact.sh bakes LINK events into a SNAPSHOT compiled_state.deps[]
    # and deletes the original *-LINK.json files.  When no active LINK file was
    # found above, scan any *-SNAPSHOT.json for a matching dep entry.  A link
    # cancelled post-compaction will have an UNLINK event on disk (not compacted)
    # — subtract those via cancelled_uuids before trusting a SNAPSHOT dep.
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
) -> None:
    """Write a single LINK event to source_id's directory (no cycle check, no idempotency).

    Routes through the ONE canonical locked write path — the shared leaf-write seam
    ``rebar._commands._seam.append_event`` → ``rebar._store.event_append.write_and_push``.
    The seam composes the canonical envelope (real ``author`` + ``env_id``, monotonic
    HLC tick) and the store core owns the dual-leg fcntl+mkdir lock, atomic rename,
    rebase guard, commit, and best-effort push. Previously this function hand-rolled
    its own ``flock`` + ``git add``/``commit`` + push-retry loop — a second write path
    that diverged from the store core (wrong author/env_id sentinels, weaker lock, no
    rebase guard). See epic ``clumsy-jab-yacht`` / story ``scabby-slur-junk``.

    Raises :class:`rebar._commands._seam.CommandError` on a genuine commit failure
    (e.g. rebase-in-progress guard, exit 75); the push step is best-effort and never
    raises. Callers tolerate this: ``link_core`` documents "Raises CommandError" and
    the reconciler's inbound applier wraps ``rebar.link`` in a non-fatal try/except.
    """
    from pathlib import Path

    from rebar._commands import _seam

    _seam.append_event(
        source_id,
        "LINK",
        {"target_id": target_id, "relation": relation},
        Path(tracker_dir),
    )


def add_dependency(
    source_id: str,
    target_id: str,
    tracker_dir: str,
    relation: str = "blocks",
) -> None:
    """Add a dependency from source_id to target_id with cycle check.

    Raises CyclicDependencyError if adding this dependency would create a cycle.
    Raises ValueError if relation is not in CANONICAL_RELATIONS.
    Writes a LINK event to the source ticket's directory.
    Idempotent: if a net-active LINK with the same (target_id, relation) already exists,
    this is a no-op (exits cleanly without writing a duplicate event).
    For relates_to: also writes a reciprocal LINK event in target_id's directory.
    """
    # Step 0: Validate relation grammar before touching disk
    if relation not in CANONICAL_RELATIONS:
        canonical_list = ", ".join(sorted(CANONICAL_RELATIONS))
        raise ValueError(f"invalid relation '{relation}': must be one of {canonical_list}")

    # Step 1: Resolve hierarchy. The relation is passed through so the resolver
    # can gate promotion: only blocking deps (blocks/depends_on) are promoted to
    # a comparable type-tier; all other relations link the exact pair.
    hierarchy_result = resolve_hierarchy_link(source_id, target_id, tracker_dir, relation)

    if "error" in hierarchy_result:
        raise ValueError(hierarchy_result["error"])

    if hierarchy_result.get("is_redundant"):
        msg = (
            f"ERROR: redundant link — {source_id} and {target_id} are in a direct "
            "parent-child relationship"
        )
        print(msg, file=sys.stderr)
        raise ValueError(msg)

    resolved_source = str(hierarchy_result["resolved_source"])
    resolved_target = str(hierarchy_result["resolved_target"])
    was_redirected = bool(hierarchy_result.get("was_redirected"))

    if was_redirected:
        print(
            f"REDIRECT: {source_id}\u2192{target_id} promoted to "
            f"{resolved_source}\u2192{resolved_target}",
            file=sys.stderr,
        )
        print(
            json.dumps(
                {
                    "redirected": True,
                    "original": {"source": source_id, "target": target_id},
                    "resolved": {"source": resolved_source, "target": resolved_target},
                }
            )
        )

    source_id = resolved_source
    target_id = resolved_target

    if check_would_create_cycle(source_id, target_id, relation, tracker_dir):
        raise CyclicDependencyError(
            f"Adding {resolved_source} → {resolved_target} ({relation}) would create a cycle"
        )

    resolved_source_dir = os.path.join(tracker_dir, resolved_source)
    resolved_source_state = (
        reduce_ticket(resolved_source_dir) if os.path.isdir(resolved_source_dir) else None
    )
    level = (
        (resolved_source_state.get("ticket_type") or "").lower() if resolved_source_state else ""
    )
    # Only the cycle-capable relations (blocks / depends_on) are subject to the
    # per-level cycle guard; relates_to / duplicates / supersedes / discovered_from
    # are non-blocking and never cycle-inducing (mirrors check_would_create_cycle).
    if (
        relation in ("blocks", "depends_on")
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

    if _is_active_link(source_id, target_id, relation, tracker_dir):
        return

    _write_link_event(source_id, target_id, relation, tracker_dir)

    if relation == "relates_to" and not _is_active_link(
        target_id, source_id, relation, tracker_dir
    ):
        _write_link_event(target_id, source_id, relation, tracker_dir)
