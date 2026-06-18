"""Streaming NDJSON export (P1.2): one full ticket object per line.

NDJSON serves both consumers from one artifact: reporting (row-oriented,
stream-parseable) and migration (each line re-imports as a full ticket). Each
line carries a ``schema_version``; run metadata (``exported_at``, ``source_env``,
counts) is returned to the caller / written to stderr by the CLI, never mixed into
a stdout data line.

Streaming is deliberate: we iterate ticket-id directories and ``reduce_ticket``
each one, writing and releasing a single line at a time. We do NOT use
``reduce_all_tickets`` — it materializes every ticket's compiled state in memory
at once, which is a memory risk on large stores. Per-ticket replay (cache +
SNAPSHOT-bounded) keeps memory flat regardless of store size.
"""

from __future__ import annotations

import json
import os
import time
from collections.abc import Iterator
from typing import Any

from rebar import config
from rebar.reducer import reduce_ticket
from rebar.reducer._present import public_state

from . import _strip

# Export-line format version (independent of the event-log SCHEMA_VERSION). Bump
# when the per-line export shape changes in a way importers must be aware of.
EXPORT_SCHEMA_VERSION = 1


def _ticket_dir_names(tracker: str) -> list[str]:
    """Sorted ticket-id directory names under ``tracker`` (cheap; no replay)."""
    try:
        entries = sorted(os.listdir(tracker))
    except OSError:
        return []
    out: list[str] = []
    for entry in entries:
        if entry.startswith("."):
            continue
        if os.path.isdir(os.path.join(tracker, entry)):
            out.append(entry)
    return out


def _csv_set(value: Any) -> set[str] | None:
    """Normalize a comma-list / iterable filter value to a set (None if empty)."""
    if value is None:
        return None
    items = value if isinstance(value, (set, list, tuple)) else str(value).split(",")
    norm = {str(v).strip() for v in items if str(v).strip()}
    return norm or None


def _source_env(tracker: str) -> str | None:
    """This store's environment id (``.env-id``), best-effort."""
    try:
        return (
            (open(os.path.join(tracker, ".env-id"), encoding="utf-8").read().strip()) or None  # noqa: SIM115
        )
    except OSError:
        return None


def iter_export_states(
    *,
    tracker: str,
    status: Any = None,
    ticket_type: Any = None,
    parent: str | None = None,
    strip_external: bool = False,
    include_session_logs: bool = False,
    exclude_archived: bool = False,
    include_deleted: bool = False,
) -> Iterator[dict]:
    """Yield export-ready ticket-state dicts (one per ticket) honoring scope flags.

    Scope defaults (P1.2): all work types & statuses incl. closed; session_log
    EXCLUDED (opt in with ``include_session_logs``); archived INCLUDED with its
    ``archived: true`` marker (opt out with ``exclude_archived``); deleted EXCLUDED
    (opt in with ``include_deleted``). ``status`` / ``ticket_type`` accept a
    comma-list or iterable; ``parent`` matches ``parent_id`` exactly.
    """
    status_filter = _csv_set(status)
    type_filter = _csv_set(ticket_type)

    for tid in _ticket_dir_names(tracker):
        state = reduce_ticket(os.path.join(tracker, tid))
        if not state:
            continue
        # Skip reducer error states (corrupt / no real CREATE) — not exportable.
        if state.get("error") or not state.get("ticket_type"):
            continue

        ttype = state.get("ticket_type")
        st = state.get("status")
        is_archived = bool(state.get("archived")) or st == "archived"

        if ttype == "session_log" and not include_session_logs:
            continue
        if st == "deleted" and not include_deleted:
            continue
        if is_archived and exclude_archived:
            continue
        if type_filter is not None and ttype not in type_filter:
            continue
        if status_filter is not None and st not in status_filter:
            continue
        if parent is not None and (state.get("parent_id") or "") != parent:
            continue

        line = public_state(state)
        if strip_external:
            line = _strip.strip_external(line)
        line["schema_version"] = EXPORT_SCHEMA_VERSION
        yield line


def export_tickets(
    *,
    out=None,
    status: Any = None,
    ticket_type: Any = None,
    parent: str | None = None,
    strip_external: bool = False,
    include_session_logs: bool = False,
    exclude_archived: bool = False,
    include_deleted: bool = False,
    repo_root=None,
) -> dict:
    """Export the store as NDJSON to ``out``; return run metadata.

    ``out`` is a writable text file object, a path (str / os.PathLike), or None for
    a returned-only run (no write). Returns
    ``{"exported", "schema_version", "source_env", "exported_at"}``. Streams one
    line at a time (bounded memory) — see module docstring.
    """
    tracker = str(config.tracker_dir(repo_root))

    opened = None
    if out is None:
        sink = None
    elif hasattr(out, "write"):
        sink = out
    else:
        opened = open(out, "w", encoding="utf-8")  # noqa: SIM115 — closed in finally
        sink = opened

    exported = 0
    try:
        for line in iter_export_states(
            tracker=tracker,
            status=status,
            ticket_type=ticket_type,
            parent=parent,
            strip_external=strip_external,
            include_session_logs=include_session_logs,
            exclude_archived=exclude_archived,
            include_deleted=include_deleted,
        ):
            if sink is not None:
                sink.write(json.dumps(line, ensure_ascii=False) + "\n")
            exported += 1
    finally:
        if opened is not None:
            opened.close()

    return {
        "exported": exported,
        "schema_version": EXPORT_SCHEMA_VERSION,
        "source_env": _source_env(tracker),
        "exported_at": time.time_ns(),
    }
