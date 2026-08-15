"""Link event writing and add_dependency for ticket-graph."""

from __future__ import annotations

import glob as _glob
import json
import logging
import os
from collections.abc import Callable

from rebar.reducer._sort import prefix_ts as _prefix_ts

from ._graph import check_cycle_at_level, check_would_create_cycle
from ._hierarchy import resolve_hierarchy_link
from ._loader import reduce_ticket
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
    """Add a dependency from source_id to target_id with cycle check.

    Raises CyclicDependencyError if adding this dependency would create a cycle.
    Raises ValueError if relation is not in CANONICAL_RELATIONS.
    Writes a LINK event to the source ticket's directory.
    Idempotent: if a net-active LINK with the same (target_id, relation) already exists,
    this is a no-op (exits cleanly without writing a duplicate event).
    For relates_to: also writes a reciprocal LINK event in target_id's directory.

    Returns the REDIRECT record when hierarchy escalation moved either endpoint, else
    None. stdout is NOT the only channel any more: the CLI still gets the printed
    record, but the library facade suppresses stdout (composer.link_core(quiet=True))
    because rebar-mcp speaks MCP-over-stdio and a stray print would corrupt the
    JSON-RPC stream. Returning it lets those callers report the substitution instead
    of silently recording a different edge (bug 1803-df54-18bb-4881).

    ``on_outcome`` (ticket 6bda-9d58-8546-4638) is the PARALLEL wrote-vs-noop channel:
    the ``dict | None`` return above is a consumed contract (REDIRECT record or not)
    that cannot distinguish a fresh write from the idempotent no-op, so when a
    callable is given it is invoked exactly once, just before return, with
    ``{"wrote", "source", "target", "relation"}`` (resolved endpoints). The return
    value's type and meaning are unchanged.
    """
    # Steps 0–1: validate relation grammar + resolve hierarchy promotion, composing
    # the machine-readable REDIRECT record — but DEFER emitting it to stdout until
    # AFTER the durable LINK commit below (bug hulky-bag-aisle). The emit previously
    # ran here, before the write — so a reader closing the pipe early
    # (`rebar link ... | head`) raised BrokenPipeError and aborted the function
    # before it committed, silently losing the link (exit status masked by the
    # pipe). Durable data first, user-facing chatter second.
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

    _write_link_event(source_id, target_id, relation, tracker_dir)

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
    """Remove the net-active ``(target_id, relation)`` link — ``add_dependency``'s mirror.

    The RELATION-SCOPED removal seam (bug e39f): links are written keyed on
    ``(target_id, relation)`` (see ``add_dependency``'s idempotency), so a pair can
    hold several relations at once; this removes exactly the named relation's most
    recent net-active LINK by writing an UNLINK event through the same shared
    locked write seam, leaving any other relation the pair holds untouched. For
    ``relates_to`` the reciprocal link in ``target_id``'s directory is removed too
    (mirroring the reciprocal write).

    Raises ValueError if ``relation`` is not in ``CANONICAL_RELATIONS``.
    Raises :class:`rebar._commands._seam.CommandError` when either ticket is
    missing or no net-active ``(target_id, relation)`` link exists.

    The net-effective LINK/UNLINK replay lives in ``rebar._commands.unlink``
    (lazily imported, mirroring ``_write_link_event``'s lazy seam import) so this
    seam and the CLI's ``unlink`` can never disagree about which link is removed.
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
