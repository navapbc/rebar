"""Concurrent, content-addressed snapshot reads and population.

An immutable SHA keys ``<root>/<sha>/``. Per-SHA thread locks plus a cross-process
``flock`` make population single-flight; losing a race is merely redundant. Hits touch
``mtime`` for LRU, never unreliable ``atime``, and use no PID/heartbeat leases. The
janitor renames before deletion, so open POSIX file descriptors remain readable while
failed new opens become :class:`CacheMiss`. A successful populater atomically adds its
exclusive bytes; the janitor owns reclamation and reconciliation.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Iterable
from pathlib import Path
from typing import IO

from rebar._snapshot.git_fetch import interprocess_lock
from rebar._snapshot.repo_snapshot import (
    DEFAULT_REF,
    SOURCE_ATTESTED,
    SOURCE_LOCAL,
    SnapshotHandle,
    entry_path,
    materialize,
    resolve_ref,
    store_root,
)
from rebar._store import fsutil


class CacheMiss(RuntimeError):
    """A cache entry vanished or could not be read (evicted mid-read / corrupt).

    The caller should treat this as a miss and re-acquire (which re-materializes)."""


# --------------------------------------------------------------------------------------
# In-process single-flight: one lock per SHA so concurrent same-SHA requests in this
# process collapse to a single materialization (the cross-process flock handles peers).
# --------------------------------------------------------------------------------------
_sha_locks: dict[str, threading.Lock] = {}
_sha_locks_guard = threading.Lock()


def _sha_lock(sha: str) -> threading.Lock:
    with _sha_locks_guard:
        lk = _sha_locks.get(sha)
        if lk is None:
            lk = threading.Lock()
            _sha_locks[sha] = lk
        return lk


def _sha_lock_path(root: Path, sha: str) -> Path:
    return root / "locks" / f"{sha}.lock"


# --------------------------------------------------------------------------------------
# Recency (touch-on-read mtime) — the janitor's LRU signal.
# --------------------------------------------------------------------------------------
def touch_entry(path: Path) -> None:
    """Mark a cache entry as just-used by bumping its ``mtime`` to now.

    Recency is tracked by ``mtime`` (set explicitly here on every hit), never ``atime``,
    which the kernel may not update under ``relatime``/``noatime`` mounts."""
    try:
        os.utime(path, None)
    except OSError:  # pragma: no cover - best effort
        pass


def entry_mtime(path: Path) -> float:
    """The entry's recency signal (``mtime``); ``0.0`` if it is gone."""
    try:
        return path.stat().st_mtime
    except OSError:
        return 0.0


# --------------------------------------------------------------------------------------
# Byte accounting — incrementally maintained so the janitor never needs a hot-path `du`.
# --------------------------------------------------------------------------------------
def _byte_total_path(root: Path) -> Path:
    return root / "bytes.total"


def byte_total(root: Path | None = None) -> int:
    root = root or store_root()
    try:
        return int(_byte_total_path(root).read_text().strip() or "0")
    except (OSError, ValueError):
        return 0


def add_bytes(delta: int, root: Path | None = None) -> int:
    """Atomically add ``delta`` to the store's running byte total; return the new total.

    Serialized cross-process by an flock so a concurrent populate (increment) and the
    janitor's decrement cannot lose an update (no read-modify-write TOCTOU)."""
    root = root or store_root()
    path = _byte_total_path(root)
    with interprocess_lock(root / "locks" / "bytes.total.lock"):
        try:
            current = int(path.read_text().strip() or "0")
        except (OSError, ValueError):
            current = 0
        new = max(0, current + delta)
        fsutil.atomic_write(path, str(new))
    return new


def entry_size(path: Path) -> int:
    """Return an entry's apparent bytes, counting shared hardlinks in every entry.

    This reporting size is unsuitable for accounting or reclamation; use
    :func:`exclusive_size` there and :func:`distinct_bytes` for store occupancy."""
    total = 0
    for dirpath, _dirnames, filenames in os.walk(path):
        for name in filenames:
            try:
                total += os.lstat(os.path.join(dirpath, name)).st_size
            except OSError:  # pragma: no cover - racing eviction
                pass
    return total


def exclusive_size(path: Path) -> int:
    """Return bytes unique to this entry: files whose ``st_nlink`` is one.

    Population thereby adds only first-link bytes and eviction subtracts only last-link
    bytes, keeping the running total aligned with :func:`distinct_bytes`. Shared donor
    hardlinks count zero. Apportioning ``st_size // st_nlink`` is wrong because it credits
    bytes that remain on disk."""
    total = 0
    for dirpath, _dirnames, filenames in os.walk(path):
        for name in filenames:
            try:
                st = os.lstat(os.path.join(dirpath, name))
            except OSError:  # pragma: no cover - racing eviction
                continue
            if st.st_nlink == 1:
                total += st.st_size
    return total


def distinct_bytes(paths: Iterable[Path]) -> int:
    """Bytes ``paths`` really occupy, charging every distinct inode exactly once.

    The authoritative ground truth the janitor's startup sweep reconciles to. Summing
    :func:`entry_size` over entries would over-count every shared blob once per entry that
    links it."""
    seen: set[tuple[int, int]] = set()
    total = 0
    for path in paths:
        for dirpath, _dirnames, filenames in os.walk(path):
            for name in filenames:
                try:
                    st = os.lstat(os.path.join(dirpath, name))
                except OSError:  # pragma: no cover - racing eviction
                    continue
                key = (st.st_dev, st.st_ino)
                if key in seen:
                    continue
                seen.add(key)
                total += st.st_size
    return total


# --------------------------------------------------------------------------------------
# Reader-safe file access.
# --------------------------------------------------------------------------------------
def open_in_snapshot(handle: SnapshotHandle, relpath: str, mode: str = "rb") -> IO[bytes]:
    """Open a snapshot file, raising :class:`CacheMiss` if its entry is unreadable.

    An already-open fd remains valid across POSIX rename-and-delete eviction."""
    target = Path(handle.path) / relpath
    try:
        return open(target, mode)
    except FileNotFoundError as exc:
        raise CacheMiss(f"snapshot entry vanished while reading {relpath!r}") from exc
    except OSError as exc:
        raise CacheMiss(f"snapshot entry unreadable for {relpath!r}: {exc}") from exc


# --------------------------------------------------------------------------------------
# The cached acquire entry point (what the gates call instead of S1.materialize).
# --------------------------------------------------------------------------------------
def acquire(
    ref: str = DEFAULT_REF,
    *,
    source_mode: str = SOURCE_ATTESTED,
    repo_root: str | None = None,
    fetch: bool = True,
) -> SnapshotHandle:
    """Return a cached handle for ``ref``, populating it single-flight when absent.

    ``local`` returns the uncached, unsigned checkout. ``attested`` resolves one immutable
    SHA, touches hits, and on a miss rechecks under thread and process locks before atomic
    materialization and one-time byte accounting."""
    if source_mode == SOURCE_LOCAL:
        return materialize(source_mode=SOURCE_LOCAL, repo_root=repo_root)

    # This fetch backs materialization, so request blobs now; S1 then runs with fetch=False.
    # A blobless fetch would force the plumbing to lazy-fetch each file.
    sha = resolve_ref(ref, repo_root, fetch=fetch, blobless=False)
    root = store_root()
    dest = entry_path(sha, root)

    if dest.is_dir():
        touch_entry(dest)
        return materialize(sha, source_mode=SOURCE_ATTESTED, repo_root=repo_root, fetch=False)

    # Single-flight: in-process per-SHA lock, then cross-process flock. Re-check existence
    # after EACH acquisition — a peer thread/process may have populated it while we waited.
    with _sha_lock(sha):
        if dest.is_dir():
            touch_entry(dest)
            return materialize(sha, source_mode=SOURCE_ATTESTED, repo_root=repo_root, fetch=False)
        with interprocess_lock(_sha_lock_path(root, sha)):
            if dest.is_dir():
                touch_entry(dest)
                return materialize(
                    sha, source_mode=SOURCE_ATTESTED, repo_root=repo_root, fetch=False
                )
            handle = materialize(sha, source_mode=SOURCE_ATTESTED, repo_root=repo_root, fetch=False)
            # We performed the populate — account its bytes exactly once.
            add_bytes(exclusive_size(dest), root)
            return handle
