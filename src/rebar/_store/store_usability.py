"""Share one store-usability test between library reads and committed writes.

A directory is usable when it has committed store structure (the compatibility
record, including zero-ticket stores, or an older event directory), or a real
``.git`` whose HEAD resolves. This admits materialized read-only snapshots
without ``.git`` while rejecting marker-only and incomplete clone directories.
Writes also require ``.git``; their separate ``.env-id`` provenance gate remains.

The HEAD probe uses bounded git because writes call it under the store lock.
A nonzero exit means unresolved HEAD; an ``OSError`` still surfaces as an
environment fault. ``tests/unit/test_reads_uninitialized_store.py`` guards both
readable snapshots and broken stores.
"""

from __future__ import annotations

import os

from rebar._store.compat import COMPAT_FILENAME
from rebar._store.gitutil import run_git_bounded

# Bound the HEAD probe even when it runs under the store write lock.
_GIT_TIMEOUT = 30


def _has_git(tracker: str) -> bool:
    """Accept ``.git`` as a directory or linked-worktree file; writes need either."""
    return os.path.exists(os.path.join(tracker, ".git"))


def _head_resolves(tracker: str) -> bool:
    """Probe this repository's HEAD with bounded git after confirming ``.git``.

    A timeout is unresolved; a missing git executable raises ``OSError``.
    """
    result = run_git_bounded(tracker, "rev-parse", "--verify", "-q", "HEAD", timeout=_GIT_TIMEOUT)
    return result.returncode == 0


def _looks_like_event_dir(path: str) -> bool:
    """Require an event ``.json`` file, not merely an empty or stray directory.

    Unreadable or non-directory entries do not qualify.
    """
    try:
        return any(name.endswith(".json") for name in os.listdir(path))
    except OSError:
        return False


def _carries_store_structure(tracker: str) -> bool:
    """Find a committed compatibility record or a non-dot event directory.

    Both identify readable stores without ``.git``; marker-only directories fail.
    """
    if os.path.exists(os.path.join(tracker, COMPAT_FILENAME)):
        return True
    try:
        entries = os.listdir(tracker)
    except OSError:
        return False
    return any(
        not entry.startswith(".") and _looks_like_event_dir(os.path.join(tracker, entry))
        for entry in entries
    )


def store_is_usable(tracker: str) -> bool:
    """Accept on-disk store structure or a repository with resolvable HEAD.

    Check ``.git`` before probing: git would otherwise walk into a parent repo
    and misclassify a broken nested tracker. Structure-first avoids routine git.
    """
    if not os.path.isdir(tracker):
        return False
    if _carries_store_structure(tracker):
        return True
    return _has_git(tracker) and _head_resolves(tracker)


def store_is_writable(tracker: str) -> bool:
    """Require a usable store with ``.git`` to commit events.

    Materialized snapshots stay readable but cannot pass this write guard.
    """
    return store_is_usable(tracker) and _has_git(tracker)
