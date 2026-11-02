"""Catch-up: make the index reflect the store, cheaply, before a query is served.

Catch-up runs SYNCHRONOUSLY in the read path. There is no daemon, no background
thread and no CI trigger, so behaviour is identical on a fresh clone, under the MCP
server, and in an environment with no CI provider at all.

It is two steps, and both are load-bearing:

1. **Enumerate** the live ticket-directory set from the store. This is what makes a
   just-created ticket discoverable by the very next query --
   :func:`~rebar.reducer.compute_dir_hash` reports change WITHIN a directory it is
   handed, but can never reveal a directory that did not exist at the last build.
2. **Diff** that set against the persisted map. Absent means new, so index it. A
   differing hash means changed, so re-index it. A directory that has gone away
   means drop its entry.

Why the diff is two-level
-------------------------

``compute_dir_hash`` is the authority on whether a ticket changed, but asking it
about every ticket means one stat per EVENT FILE, and that is what makes a warm
query unaffordable. Measured on this project's own store -- 9,875 tickets holding
141,701 event files:

- one ``listdir`` plus one stat per event file: 4.85 s
- ``os.scandir`` with ``entry.stat()``: 2.40 s
- one stat per ticket DIRECTORY: 0.051 s

Per-file statting is roughly 5x over the warm-query budget even after the obvious
optimisation, because the cost is the file count and not the call shape. So each
``dirstate`` row stores the directory's own mtime alongside its exact hash, and
catch-up stats the DIRECTORY first. An unchanged mtime means skip, with no
``listdir`` and no hashing. Only a directory whose mtime moved is hashed, and the
hash -- not the mtime -- then decides whether it is re-indexed.

The mtime is sound as a pre-filter because a directory's mtime changes whenever an
entry is added to, removed from, or renamed within it, and those are the only ways
rebar mutates a ticket: events are append-only files and retirement is a rename.
The mtime is never trusted as the authority, only as permission to skip the
expensive exact check.

The ordering is also safe under a concurrent write. The mtime is read BEFORE the
content, so a write landing in between causes the stored mtime to look stale on the
next run and the ticket is re-hashed. The failure mode is redundant work, never a
missed change.

Applying the diff
-----------------

The diff is applied IN PLACE, to the live index, inside ONE sqlite transaction. It
is never built into a replacement database:

* Unchanged entries survive **by construction** -- reconciliation only ever touches
  the rows the diff named. A fresh database built from the diff alone would discard
  every entry the diff did not mention, and copying the live file first would pay
  the store's full size on every create.
* sqlite already supplies the atomicity and crash-safety a temp-file-and-rename
  swap would have provided, so neither ``os.rename`` nor ``os.replace`` appears here
  and the difference between their overwrite semantics on POSIX and Windows cannot
  arise.
* An interrupted process rolls back and leaves the previous committed state intact.
* Concurrent reconcilers serialize on sqlite's write lock. A loser's work is redone
  rather than merged, which is safe because each recomputes the hashes it writes.

On an unchanged store every mtime matches its stored value, the diff is empty, **no
transaction is opened at all** and no FTS5 write occurs.

One run of lag is expected after any run that indexed something. Re-indexing calls the
reducer, which writes a ``.cache.json`` into the ticket directory, and that write lands
AFTER ``live_entries`` read the mtime now persisted in ``dirstate``. The next catch-up
therefore sees a moved mtime for exactly the directories it just indexed and re-hashes
them; the hash is unchanged, so nothing is re-indexed and the stored mtime converges.
The run after that is fully warm.
"""

from __future__ import annotations

import logging
import os
import sqlite3
from typing import Any

logger = logging.getLogger(__name__)

UNKNOWN_MTIME = -1
"""Stands in for a directory mtime that could not be read. Not a real mtime, and so
never equal to one and never equal to another unknown: see ``_changed``."""


def _event_filenames(ticket_dir: str) -> list[str]:
    """The active event files of one ticket dir, in the reducer's own filter order."""
    from rebar.reducer._cache import is_active_event

    try:
        names = os.listdir(ticket_dir)
    except OSError:
        return []
    return sorted(
        n for n in names if not n.startswith(".") and n.endswith(".json") and is_active_event(n)
    )


def _dir_mtime_ns(path: str) -> int:
    """The directory's own mtime, or ``UNKNOWN_MTIME`` when it cannot be stat'd."""
    try:
        return os.stat(path).st_mtime_ns
    except OSError:
        return UNKNOWN_MTIME


def dir_hash_of(ticket_dir: str) -> str:
    """The exact reducer dir hash for one ticket directory."""
    from rebar.reducer._cache import compute_dir_hash

    return compute_dir_hash(ticket_dir, _event_filenames(ticket_dir))


def live_entries(tracker: str | os.PathLike[str]) -> dict[str, int]:
    """Step 1: the live ticket-directory set mapped to its current directory mtime.

    Enumerating here -- rather than consulting only the persisted map -- is what makes
    a newly created ticket discoverable. Only the cheap mtime is collected; hashing is
    deferred to the directories that actually look touched.
    """
    from rebar._store.gitutil import _ticket_dirs

    root = str(tracker)
    return {name: _dir_mtime_ns(os.path.join(root, name)) for name in _ticket_dirs(root)}


def _document(tracker: str, dir_name: str) -> tuple[str, str, str, str] | None:
    """The indexable row for one ticket dir, or ``None`` when it will not reduce."""
    from rebar.reducer import reduce_ticket

    state = reduce_ticket(os.path.join(tracker, dir_name))
    if not state:
        return None
    return (
        str(state.get("ticket_id") or dir_name),
        str(state.get("alias") or ""),
        str(state.get("title") or ""),
        str(state.get("description") or ""),
    )


def _changed(
    tracker: str, live: dict[str, int], stored: dict[str, tuple[int, str]]
) -> dict[str, tuple[int, str]]:
    """The directories needing attention, as ``{dir_name: (mtime_ns, dir_hash)}``.

    A directory whose mtime is unchanged is skipped without hashing. One whose mtime
    moved is hashed exactly; the HASH then decides whether its document is rewritten,
    so a touched-but-identical directory costs a hash and no index write.

    ``UNKNOWN_MTIME`` is never treated as unchanged. It is a sentinel standing for "no
    mtime was readable", so two of them say nothing about whether the directory moved;
    comparing them by equality would skip the hash check and freeze a stale document
    for as long as the stat keeps failing.
    """
    out: dict[str, tuple[int, str]] = {}
    for name, mtime_ns in live.items():
        previous = stored.get(name)
        if previous is not None and mtime_ns != UNKNOWN_MTIME and previous[0] == mtime_ns:
            continue
        out[name] = (mtime_ns, dir_hash_of(os.path.join(tracker, name)))
    return out


def _reindex(conn: sqlite3.Connection, tracker: str, dir_name: str) -> int:
    """Rewrite one ticket's document. Returns 1 when a row was indexed, else 0."""
    doc = _document(tracker, dir_name)
    # FTS5 has no upsert, so re-indexing is delete-then-insert; the pair is inside the
    # caller's transaction, so no reader ever observes the gap. Both keys are cleared
    # because a ticket dir is named for its canonical id, but the reduced state is the
    # authority on that id.
    keys = {dir_name} | ({doc[0]} if doc else set())
    conn.executemany("DELETE FROM docs WHERE ticket_id = ?", [(k,) for k in keys])
    if doc is None:
        return 0
    conn.execute(
        "INSERT INTO docs (ticket_id, alias, title, description) VALUES (?, ?, ?, ?)",
        doc,
    )
    return 1


def _apply(
    conn: sqlite3.Connection,
    tracker: str,
    changed: dict[str, tuple[int, str]],
    removed: list[str],
    stored: dict[str, tuple[int, str]],
) -> tuple[int, int]:
    """Apply the diff in place, inside one transaction. Returns (indexed, dropped)."""
    indexed = 0
    conn.execute("BEGIN IMMEDIATE")
    try:
        for dir_name in removed:
            conn.execute("DELETE FROM docs WHERE ticket_id = ?", (dir_name,))
            conn.execute("DELETE FROM dirstate WHERE dir_name = ?", (dir_name,))
        for dir_name, (mtime_ns, dir_hash) in changed.items():
            previous = stored.get(dir_name)
            if previous is None or previous[1] != dir_hash:
                indexed += _reindex(conn, tracker, dir_name)
            conn.execute(
                "INSERT INTO dirstate (dir_name, dir_mtime_ns, dir_hash) VALUES (?, ?, ?) "
                "ON CONFLICT(dir_name) DO UPDATE SET "
                "  dir_mtime_ns = excluded.dir_mtime_ns, dir_hash = excluded.dir_hash",
                (dir_name, mtime_ns, dir_hash),
            )
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    return indexed, len(removed)


def catch_up(tracker: str | os.PathLike[str], conn: sqlite3.Connection) -> dict[str, Any]:
    """Reconcile the index with the store. Returns ``{indexed, dropped, scanned}``."""
    root = str(tracker)
    live = live_entries(root)
    stored = {
        row[0]: (row[1], row[2])
        for row in conn.execute("SELECT dir_name, dir_mtime_ns, dir_hash FROM dirstate").fetchall()
    }

    changed = _changed(root, live, stored)
    removed = [name for name in stored if name not in live]

    if not changed and not removed:
        # The warm path: no transaction is opened and no FTS5 write occurs.
        return {"indexed": 0, "dropped": 0, "scanned": len(live)}

    indexed, dropped = _apply(conn, root, changed, removed, stored)
    return {"indexed": indexed, "dropped": dropped, "scanned": len(live)}
