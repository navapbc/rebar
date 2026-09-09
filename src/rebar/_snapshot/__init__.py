"""Faithful, immutable repository snapshots for code-reading gates.

A long-lived server's checkout can change during a gate, so attested reads use a
client-pinned committed tree instead. :func:`materialize` returns a
:class:`SnapshotHandle` for that tree, or the untouched checkout in unsigned ``local``
mode. Handles expose the read root, pinned SHA, source mode, and detected LFS pointers
and submodules. :class:`SnapshotError`, :class:`SnapshotFetchError`, and
:class:`SnapshotRefError` provide the fail-closed error vocabulary.

:mod:`rebar._snapshot.repo_snapshot` owns materialization; sibling modules add
single-flight caching, reader safety, byte accounting, and reclamation.
"""

from __future__ import annotations

from rebar._snapshot.cache import (
    CacheMiss,
    acquire,
    add_bytes,
    byte_total,
    entry_mtime,
    entry_size,
    open_in_snapshot,
    touch_entry,
)
from rebar._snapshot.gc_trigger import maybe_gc
from rebar._snapshot.janitor import (
    JanitorConfig,
    drain_trash,
    reverify_entry,
    run_gc,
    start_background_janitor,
    startup_sweep,
)
from rebar._snapshot.repo_snapshot import (
    DEFAULT_REF,
    SOURCE_ATTESTED,
    SOURCE_LOCAL,
    SnapshotError,
    SnapshotFetchError,
    SnapshotHandle,
    SnapshotRefError,
    is_lfs_pointer,
    materialize,
    materialize_tickets,
    resolve_ref,
    store_root,
    sweep_tmp,
)

__all__ = [
    "DEFAULT_REF",
    "SOURCE_ATTESTED",
    "SOURCE_LOCAL",
    "CacheMiss",
    "JanitorConfig",
    "SnapshotError",
    "SnapshotFetchError",
    "SnapshotHandle",
    "SnapshotRefError",
    "acquire",
    "add_bytes",
    "byte_total",
    "drain_trash",
    "entry_mtime",
    "entry_size",
    "is_lfs_pointer",
    "materialize",
    "materialize_tickets",
    "maybe_gc",
    "open_in_snapshot",
    "resolve_ref",
    "reverify_entry",
    "run_gc",
    "start_background_janitor",
    "startup_sweep",
    "store_root",
    "sweep_tmp",
    "touch_entry",
]
