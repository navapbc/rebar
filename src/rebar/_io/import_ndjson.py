"""NDJSON import (P1.2): re-create a store's tickets through the locked write path.

Consumes our own export NDJSON and reproduces the same *logical* state in the
target repo by composing ordinary events (CREATE + EDIT-parent + LINK + COMMENT +
FILE_IMPACT/VERIFY_COMMANDS + STATUS) — never raw-event injection. This is a
**provenance** import, not raw fidelity: tickets get fresh ids + fresh HLC
timestamps, with the source identity preserved as ``source_*`` (see
:mod:`_provenance`).

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
import os
import sys
from collections.abc import Iterable, Iterator
from typing import Any

from . import _provenance


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
                print(f"WARNING: skipping unparseable NDJSON line: {line[:80]}", file=sys.stderr)
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


def import_tickets(source: Any, *, dry_run: bool = False, repo_root=None) -> dict:
    """Import tickets from NDJSON ``source`` into the target repo.

    Returns run metadata: ``{created, skipped, links, comments, warnings, dry_run}``
    (``warnings`` is a list of human-readable strings; also echoed to stderr).
    """
    records = list(_iter_records(source))
    warnings: list[str] = []

    def warn(msg: str) -> None:
        warnings.append(msg)
        print(f"WARNING: {msg}", file=sys.stderr)

    if dry_run:
        creatable = sum(1 for r in records if r.get("ticket_id") and r.get("ticket_type"))
        return {
            "created": 0,
            "would_create": creatable,
            "skipped": 0,
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

    rr = None if repo_root is None else str(repo_root)

    # ── Pass 1: create every ticket; capture source_id → local_id ──────────────
    id_map: dict[str, str] = {}
    created = 0
    for rec in records:
        sid = rec.get("ticket_id")
        if not sid or not rec.get("ticket_type"):
            warn(f"skipping record without ticket_id/ticket_type: {str(rec)[:80]}")
            continue
        kwargs = _provenance.create_kwargs(rec)
        local = create_ticket(repo_root=rr, **kwargs)
        id_map[sid] = local
        created += 1

    # ── Pass 2a: set parents while every ticket is still open ──────────────────
    parent_local: dict[str, str] = {}
    for rec in records:
        local = id_map.get(rec.get("ticket_id"))
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

    # ── Pass 2b: links (dedup by local (source,target,relation)) ───────────────
    links = 0
    seen_links: set[tuple[str, str, str]] = set()
    for rec in records:
        local = id_map.get(rec.get("ticket_id"))
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

    # ── Pass 2c: file-impact / verify-commands ─────────────────────────────────
    for rec in records:
        local = id_map.get(rec.get("ticket_id"))
        if local is None:
            continue
        fi = rec.get("file_impact")
        if fi:
            try:
                set_file_impact(local, fi, repo_root=rr)
            except Exception as exc:  # noqa: BLE001
                warn(f"could not set file_impact on {local}: {exc}")
        vc = rec.get("verify_commands")
        if vc:
            try:
                set_verify_commands(local, vc, repo_root=rr)
            except Exception as exc:  # noqa: BLE001
                warn(f"could not set verify_commands on {local}: {exc}")

    # ── Pass 2d: comments (with provenance) ────────────────────────────────────
    comments = 0
    for rec in records:
        local = id_map.get(rec.get("ticket_id"))
        if local is None:
            continue
        for entry in rec.get("comments") or []:
            body = entry.get("body")
            if not body:
                continue
            try:
                comment(local, body, source=_provenance.comment_source(entry), repo_root=rr)
                comments += 1
            except Exception as exc:  # noqa: BLE001
                warn(f"could not add comment on {local}: {exc}")

    # ── Pass 2e: statuses last (children before parents; archived via archive) ──
    closes: list[tuple[str, str]] = []  # (local_id, source_id)
    for rec in records:
        local = id_map.get(rec.get("ticket_id"))
        if local is None:
            continue
        status = rec.get("status")
        if status in ("in_progress", "blocked"):
            try:
                transition_compute(local, "open", status, repo_root=rr)
            except Exception as exc:  # noqa: BLE001
                warn(f"could not set {local} to {status}: {exc}")
        elif status == "archived" or rec.get("archived"):
            try:
                archive(local, repo_root=rr)
            except Exception as exc:  # noqa: BLE001
                warn(f"could not archive {local}: {exc}")
        elif status == "closed":
            closes.append((local, rec.get("ticket_id")))

    # Close children before parents so the open-children guard is satisfied;
    # force=True is a safety net for a genuinely-non-closed child in the source.
    closes.sort(key=lambda pair: _local_depth(pair[0], parent_local), reverse=True)
    for local, _sid in closes:
        # Every closeable ticket is still 'open' here (only in_progress/blocked/
        # archived were set above); force is a safety net for non-closed children.
        try:
            transition_compute(local, "open", "closed", force=True, repo_root=rr)
        except Exception as exc:  # noqa: BLE001
            warn(f"could not close {local}: {exc}")

    return {
        "created": created,
        "skipped": 0,
        "links": links,
        "comments": comments,
        "warnings": warnings,
        "dry_run": False,
    }
