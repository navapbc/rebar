"""Repo-snapshot isolation for the code-reading gates (epic ``raze-vet-ditch``).

The rebar MCP server is a long-lived process pinned to ONE working directory, so a
gate that reads project source from that mutable checkout reads whatever branch +
uncommitted edits happen to be present at call time — which produced a false-negative
completion verdict when a parallel task switched the shared checkout. This package
gives every code-reading gate a *faithful, immutable, reproducible* on-disk view of
the repository at a client-pinned ref instead.

Public surface (the seam the gates / signing consume):

* :func:`materialize` — resolve a client ``ref`` to an immutable SHA and materialize a
  faithful snapshot of the committed tree (``attested`` mode), or hand back the
  in-place checkout untouched (``local`` mode). Returns a context-managed
  :class:`SnapshotHandle`.
* :class:`SnapshotHandle` — ``path`` (the read root), ``sha`` (the pinned SHA, ``None``
  in local mode), ``source`` mode, plus detected ``lfs_pointers`` / ``submodules``.
* :class:`SnapshotError` (+ :class:`SnapshotFetchError`, :class:`SnapshotRefError`) —
  the descriptive, fail-closed error vocabulary.

S1 (this module, :mod:`rebar._snapshot.repo_snapshot`) is the materialization *core*.
The content-addressed cache (single-flight, reader-safety, byte accounting) and the
reclamation janitor are layered on top in sibling modules.
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
from rebar._snapshot.repo_snapshot import (
    SnapshotError,
    SnapshotFetchError,
    SnapshotHandle,
    SnapshotRefError,
    is_lfs_pointer,
    materialize,
    resolve_ref,
    store_root,
    sweep_tmp,
)

__all__ = [
    "CacheMiss",
    "SnapshotError",
    "SnapshotFetchError",
    "SnapshotHandle",
    "SnapshotRefError",
    "acquire",
    "add_bytes",
    "byte_total",
    "entry_mtime",
    "entry_size",
    "is_lfs_pointer",
    "materialize",
    "open_in_snapshot",
    "resolve_ref",
    "store_root",
    "sweep_tmp",
    "touch_entry",
]
