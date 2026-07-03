"""Content-addressed snapshot cache — the read/populate path (epic ``raze-vet-ditch`` S2).

The same SHA is often requested by several concurrent gates; an immutable SHA is a
perfect cache key (no staleness), so ``<root>/<sha>/`` is content-addressed. This layer
sits on top of the S1 materialization core (:mod:`rebar._snapshot.repo_snapshot`) and
adds the safe-concurrent **read/populate** path:

* **Single-flight** per SHA — an in-process per-SHA lock collapses concurrent same-SHA
  requests to ONE materialization; an additional cross-process ``flock(LOCK_EX)`` on
  ``locks/<sha>.lock`` collapses racing *processes* too. A lost race is only ever
  *wasteful* (a redundant build of identical content), never *wrong*.
* **Reader safety via POSIX delete-on-last-close** — eviction (the sibling janitor)
  renames an entry away and ``rmtree``s it, NEVER deletes in place; a reader holding an
  open fd keeps reading the evicted content, and a *new* lookup that hits ``ENOENT``/a
  read error treats it as a miss and re-materializes (:class:`CacheMiss`).
* **Recency by touch-on-read ``mtime``** — every cache hit ``utime``s the entry so the
  janitor can evict LRU by ``mtime`` (NEVER ``atime``, which is unreliable under
  ``relatime``/``noatime``). There is deliberately **no PID/heartbeat lease** anywhere.
* **Byte accounting** — the store's running byte total is incremented atomically (under
  an flock) when THIS caller populates an entry; the janitor reconciles/decrements it.

Reclamation/eviction/disk-pressure handling is the sibling janitor story — this module
only populates, reads, accounts, and records recency.
"""

from __future__ import annotations

import os
import threading
from pathlib import Path

from rebar._snapshot.repo_snapshot import (
    DEFAULT_REF,
    SOURCE_ATTESTED,
    SOURCE_LOCAL,
    SnapshotHandle,
    _interprocess_lock,
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
    with _interprocess_lock(root / "locks" / "bytes.total.lock"):
        try:
            current = int(path.read_text().strip() or "0")
        except (OSError, ValueError):
            current = 0
        new = max(0, current + delta)
        fsutil.atomic_write(path, str(new))
    return new


def entry_size(path: Path) -> int:
    """Total bytes of a materialized entry (one walk; used to account a populate)."""
    total = 0
    for dirpath, _dirnames, filenames in os.walk(path):
        for name in filenames:
            try:
                total += os.lstat(os.path.join(dirpath, name)).st_size
            except OSError:  # pragma: no cover - racing eviction
                pass
    return total


# --------------------------------------------------------------------------------------
# Reader-safe file access.
# --------------------------------------------------------------------------------------
def open_in_snapshot(handle: SnapshotHandle, relpath: str, mode: str = "rb"):
    """Open a file under a snapshot, translating a vanished/unreadable entry into a
    :class:`CacheMiss` (the entry was evicted mid-read — re-acquire and retry).

    Open the returned fd up front and keep reading it: POSIX delete-on-last-close means
    the content stays readable even if the janitor evicts the entry while you hold it."""
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
    """Return a :class:`SnapshotHandle` for ``ref``, single-flight populating the cache.

    ``local`` mode passes straight through to the in-place checkout (never cached, never
    signable). ``attested`` resolves ``ref`` to an immutable SHA (one coalesced fetch),
    then: cache hit → touch recency + return; cache miss → take the in-process + cross-
    process single-flight locks, re-check, materialize via S1 (atomic populate), and
    account the populated bytes exactly once."""
    if source_mode == SOURCE_LOCAL:
        return materialize(source_mode=SOURCE_LOCAL, repo_root=repo_root)

    sha = resolve_ref(ref, repo_root, fetch=fetch)
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
        with _interprocess_lock(_sha_lock_path(root, sha)):
            if dest.is_dir():
                touch_entry(dest)
                return materialize(
                    sha, source_mode=SOURCE_ATTESTED, repo_root=repo_root, fetch=False
                )
            handle = materialize(sha, source_mode=SOURCE_ATTESTED, repo_root=repo_root, fetch=False)
            # We performed the populate — account its bytes exactly once.
            add_bytes(entry_size(dest), root)
            return handle
