"""NDJSON import (P1.2): re-create a store's tickets through the locked write path.

Consumes our own export NDJSON and reproduces the same *logical* state in the
target repo by composing ordinary events (CREATE + EDIT-parent + LINK + COMMENT +
FILE_IMPACT/VERIFY_COMMANDS + STATUS) — never raw-event injection. This is a
**provenance** import, not raw fidelity: tickets get fresh ids + fresh HLC
timestamps, with the source identity preserved as ``source_*`` (see
:mod:`_provenance`).

Import is the highest-volume LOCAL writer. Any commit-batching for imports belongs
to the ``rebar._store`` write path — NOT the Jira reconciler's inbound batcher (a
separate system). See ``docs/architecture.md`` "Two writers, one store".

Two passes:

* **Pass 1 — create.** Every record becomes a ticket; we capture
  ``{source_id → local_id}``. Parent is deliberately NOT set here (the parent may
  not exist yet, and ``edit`` refuses a closed parent).
* **Pass 2 — wire up,** in sub-phases ordered so each step's preconditions hold:
  parents (while everything is still open) → links (so blocking-link promotion can
  walk the now-set hierarchy) → file-impact / verify-commands → comments →
  statuses (last, children-before-parents so the open-children close guard is
  satisfied; ``force`` is a safety net for a genuinely-non-closed child).

A dangling parent / link target (a source id not in this import set) is skipped
with a warning — never a hard failure. Idempotent skip-by-source_id and
deferred-push performance are layered on in a later sub-task; this importer always
creates (a re-run duplicates — documented).
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Iterable, Iterator
from typing import Any, cast

from . import _provenance

logger = logging.getLogger(__name__)


def _iter_records(source: Any) -> Iterator[dict]:
    """Yield ticket dicts from an NDJSON source: a path, a file object, or an
    iterable of lines / dicts. Blank lines are skipped; an unparseable line is
    warned-and-skipped (robust import, never abort)."""
    if isinstance(source, (str, os.PathLike)):
        with open(source, encoding="utf-8") as fh:
            yield from _iter_records(fh)
        return
    if isinstance(source, Iterable):
        for item in source:
            if isinstance(item, dict):
                yield item
                continue
            line = str(item).strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                logger.warning("skipping unparseable NDJSON line: %s", line[:80], exc_info=True)
        return
    raise TypeError(f"unsupported import source: {type(source).__name__}")


def _local_depth(local_id: str, parent_local: dict[str, str]) -> int:
    """Depth of a ticket in the local parent forest (root = 0), cycle-safe."""
    depth = 0
    seen: set[str] = set()
    cur = parent_local.get(local_id)
    while cur is not None and cur not in seen:
        seen.add(cur)
        depth += 1
        cur = parent_local.get(cur)
    return depth


def _scan_existing_source_ids(tracker: str) -> dict[str, str]:
    """Map ``source_id → local ticket_id`` for tickets already imported into the
    target store. One streaming scan (one ``reduce_ticket`` per dir) at import start
    so a re-run / resume-after-crash never duplicates: a record whose ``source_id``
    is already present is skipped (existing tickets are never updated — that is sync,
    out of scope)."""
    from rebar.reducer import reduce_ticket

    from .export_ndjson import _ticket_dir_names

    out: dict[str, str] = {}
    for name in _ticket_dir_names(tracker):
        state = reduce_ticket(os.path.join(tracker, name))
        if state and state.get("source_id"):
            out[str(state["source_id"])] = state.get("ticket_id") or name
    return out


def _rec_sid(rec: dict) -> str:
    """Return a record's source ``ticket_id`` as ``str``.

    ``ticket_id`` is JSON-sourced (statically ``Any | None``); every record that
    reaches pass 2 (``created_records``) was validated to carry a truthy
    ``ticket_id`` in pass 1, so the narrowing cast is honest, not a silence.
    """
    return cast(str, rec.get("ticket_id"))


def import_tickets(source: Any, *, dry_run: bool = False, repo_root=None) -> dict:
    """Import tickets from NDJSON ``source`` into the target repo.

    Idempotent: a streaming scan of the target builds ``{source_id → local_id}`` and
    any record whose ``source_id`` already exists is SKIPPED (never updated). A
    re-run or resume-after-crash therefore produces zero duplicates. Push is deferred
    for the duration (``REBAR_SYNC_PUSH=off``) and a single push runs at the end, so
    a bulk import pays one network round-trip instead of one per event.

    Returns run metadata
    ``{created, skipped, links, comments, warnings, dry_run}`` (``warnings`` is a
    list of human-readable strings; also echoed to stderr).
    """
    from rebar import config

    records = list(_iter_records(source))
    warnings: list[str] = []

    def warn(msg: str) -> None:
        warnings.append(msg)
        logger.warning("%s", msg)

    rr = None if repo_root is None else str(repo_root)
    tracker = str(config.tracker_dir(rr))
    existing = _scan_existing_source_ids(tracker)

    if dry_run:
        seen = set(existing)
        would = 0
        skipped = 0
        for rec in records:
            sid = rec.get("ticket_id")
            if not sid or not rec.get("ticket_type"):
                continue
            if sid in seen:
                skipped += 1
                continue
            seen.add(sid)
            would += 1
        return {
            "created": 0,
            "would_create": would,
            "skipped": skipped,
            "links": 0,
            "comments": 0,
            "warnings": warnings,
            "dry_run": True,
        }

    from rebar import (
        archive,
        comment,
        create_ticket,
        edit_ticket,
        link,
        set_file_impact,
        set_verify_commands,
    )
    from rebar._commands.transition import transition_compute

    # id_map seeds with pre-existing source→local mappings so a NEW ticket can still
    # parent/link onto an already-imported one; only freshly-created records are
    # mutated in pass 2 (existing tickets are never updated).
    id_map: dict[str, str] = dict(existing)
    seen_source: set[str] = set(existing)
    parent_local: dict[str, str] = {}
    created_records: list[dict] = []
    created = 0
    skipped = 0
    links = 0
    comments = 0

    # Defer push: one final push instead of one per event (eliminates per-event
    # network round-trips). Restored in finally; the final push then honors the
    # caller's real sync.push policy.
    _prev_push = os.environ.get("REBAR_SYNC_PUSH")
    os.environ["REBAR_SYNC_PUSH"] = "off"
    try:
        # ── Pass 1: create new tickets; skip already-imported by source_id ─────
        for rec in records:
            sid = rec.get("ticket_id")
            if not sid or not rec.get("ticket_type"):
                warn(f"skipping record without ticket_id/ticket_type: {str(rec)[:80]}")
                continue
            if sid in seen_source:
                skipped += 1
                continue
            local = create_ticket(repo_root=rr, **_provenance.create_kwargs(rec))
            id_map[sid] = local
            seen_source.add(sid)
            created += 1
            created_records.append(rec)

        # ── Pass 2a: set parents while every new ticket is still open ──────────
        for rec in created_records:
            local = id_map.get(_rec_sid(rec))
            if local is None:
                continue
            psid = rec.get("parent_id")
            if not psid:
                continue
            plocal = id_map.get(psid)
            if plocal is None:
                warn(f"dangling parent {psid!r} for {rec.get('ticket_id')!r} — left unparented")
                continue
            edit_ticket(local, parent=plocal, repo_root=rr)
            parent_local[local] = plocal

        # ── Pass 2b: links (dedup by local (source,target,relation)) ───────────
        seen_links: set[tuple[str, str, str]] = set()
        for rec in created_records:
            local = id_map.get(_rec_sid(rec))
            if local is None:
                continue
            for dep in rec.get("deps") or []:
                tsid = dep.get("target_id")
                relation = dep.get("relation")
                if not tsid or not relation:
                    continue
                tlocal = id_map.get(tsid)
                if tlocal is None:
                    warn(
                        f"dangling link target {tsid!r} ({relation}) "
                        f"from {rec.get('ticket_id')!r} — skipped"
                    )
                    continue
                key = (local, tlocal, relation)
                if key in seen_links:
                    continue
                seen_links.add(key)
                try:
                    link(local, tlocal, relation, repo_root=rr)
                    links += 1
                except Exception as exc:  # noqa: BLE001 — one bad link never aborts the run
                    warn(f"could not link {local}->{tlocal} ({relation}): {exc}")

        # ── Pass 2c: file-impact / verify-commands ─────────────────────────────
        for rec in created_records:
            local = id_map.get(_rec_sid(rec))
            if local is None:
                continue
            fi = rec.get("file_impact")
            if fi:
                try:
                    set_file_impact(local, fi, repo_root=rr)
                except Exception as exc:  # noqa: BLE001 — per-row fail-open: one bad file_impact never aborts the import run; collected via warn()
                    warn(f"could not set file_impact on {local}: {exc}")
            vc = rec.get("verify_commands")
            if vc:
                try:
                    set_verify_commands(local, vc, repo_root=rr)
                except Exception as exc:  # noqa: BLE001 — per-row fail-open: one bad verify_commands never aborts the import run; collected via warn()
                    warn(f"could not set verify_commands on {local}: {exc}")

        # ── Pass 2d: comments (with provenance) ────────────────────────────────
        for rec in created_records:
            local = id_map.get(_rec_sid(rec))
            if local is None:
                continue
            for entry in rec.get("comments") or []:
                body = entry.get("body")
                if not body:
                    continue
                try:
                    comment(local, body, source=_provenance.comment_source(entry), repo_root=rr)
                    comments += 1
                except Exception as exc:  # noqa: BLE001 — per-comment fail-open: one bad comment never aborts the import run; collected via warn()
                    warn(f"could not add comment on {local}: {exc}")

        # ── Pass 2e: statuses last (children before parents; archived via archive)
        closes: list[tuple[str, str]] = []  # (local_id, source_id)
        for rec in created_records:
            local = id_map.get(_rec_sid(rec))
            if local is None:
                continue
            status = rec.get("status")
            if status in ("in_progress", "blocked"):
                try:
                    # cascade=False: import replays each ticket's recorded status
                    # explicitly; the parent-first claim/transition cascade would
                    # pre-move an open parent and then conflict with that parent's
                    # own in_progress transition in this same pass.
                    transition_compute(local, "open", status, cascade=False, repo_root=rr)
                except Exception as exc:  # noqa: BLE001 — per-row fail-open: one bad status transition never aborts the import run; collected via warn()
                    warn(f"could not set {local} to {status}: {exc}")
            elif status == "archived" or rec.get("archived"):
                try:
                    archive(local, repo_root=rr)
                except Exception as exc:  # noqa: BLE001 — per-row fail-open: one bad archive never aborts the import run; collected via warn()
                    warn(f"could not archive {local}: {exc}")
            elif status == "closed":
                closes.append((local, _rec_sid(rec)))

        # Close children before parents so the open-children guard is satisfied;
        # force=True is a safety net for a genuinely-non-closed child in the source.
        closes.sort(key=lambda pair: _local_depth(pair[0], parent_local), reverse=True)
        for local, _sid in closes:
            # Every closeable ticket is still 'open' here (only in_progress/blocked/
            # archived were set above); force is a safety net for non-closed children.
            try:
                transition_compute(local, "open", "closed", force=True, repo_root=rr)
            except Exception as exc:  # noqa: BLE001 — per-row fail-open: one bad close never aborts the import run; collected via warn()
                warn(f"could not close {local}: {exc}")
    finally:
        # Restore the caller's push policy before the single final push.
        if _prev_push is None:
            os.environ.pop("REBAR_SYNC_PUSH", None)
        else:
            os.environ["REBAR_SYNC_PUSH"] = _prev_push

    # One final push for the whole import (honors the restored sync.push policy).
    if created:
        from rebar._store import push

        push.push_tickets_branch(tracker)

    return {
        "created": created,
        "skipped": skipped,
        "links": links,
        "comments": comments,
        "warnings": warnings,
        "dry_run": False,
    }
