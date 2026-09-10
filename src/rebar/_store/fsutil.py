"""Crash-atomic file writes using a unique sibling and ``os.replace``.

This low-level module gives caches, configuration, event staging, sidecars, prompts,
and agent scratch one write contract. Sibling lock helpers are imported lazily to keep
dependencies acyclic. ``mkstemp`` uses ``O_EXCL`` to create an exclusive temporary file
beside the target. ``os.replace`` then publishes it atomically on the same filesystem,
so readers observe either complete version.
Failure before replacement preserves the target and removes the temporary file.

Text mode disables newline translation and applies the requested encoding. Binary
mode writes bytes unchanged. Optional ``fsync`` persists both file content and the
directory rename across power loss. Without it, replacement remains crash-atomic
but not power-loss durable. Explicit permissions override the default mode derived
from the process umask. The parent directory must already exist.

Temporary names must not derive from the target or process identifier (ticket
b0ac-3c0f-3f64-4344). Concurrent threads would share that path, allowing one
replacement to consume another writer's temporary file. Exclusive ``mkstemp``
names preserve every writer. The audit found no root-unkeyed module or LRU cache,
so the defect class was limited to temporary-file naming.
"""

from __future__ import annotations

import os
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager

__all__ = ["atomic_write", "sibling_exclusive_lock"]


@contextmanager
def sibling_exclusive_lock(
    path: str | os.PathLike[str],
    *,
    lock_name: str | None = None,
) -> Iterator[None]:
    """Hold an exclusive kernel lock on a stable sibling lock file.

    The lock file is intentionally retained after release. A retained empty
    lock path is harmless, avoids create/unlink races, and gives every process
    a stable inode-adjacent rendezvous point for read-modify-write sidecars.
    """
    from rebar._store.lock_kernel import release_exclusive, take_blocking_exclusive

    path = os.fspath(path)
    directory = os.path.dirname(path) or "."
    os.makedirs(directory, exist_ok=True)
    base = os.path.basename(path)
    lock_path = os.path.join(directory, lock_name or f"{base}.lock")
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
    locked = False
    try:
        take_blocking_exclusive(fd)
        locked = True
        yield
    finally:
        try:
            if locked:
                release_exclusive(fd)
        finally:
            os.close(fd)


def _umask_mode() -> int:
    """The mode a fresh ``open(path, "w")`` produces: ``0o666 & ~umask``. There is no
    ``os.getumask``; the read-and-restore dance is the standard idiom."""
    m = os.umask(0)
    os.umask(m)
    return 0o666 & ~m


def atomic_write(
    path: str | os.PathLike[str],
    data: str | bytes,
    *,
    mode: str = "w",
    encoding: str = "utf-8",
    fsync: bool = False,
    permissions: int | None = None,
) -> None:
    """Atomically write ``data`` to ``path`` (temp-in-same-dir + ``os.replace``).

    ``mode`` is ``"w"`` (text — ``data`` must be ``str``) or ``"wb"`` (bytes — ``data``
    must be ``bytes``). ``encoding`` applies to text. ``fsync=True`` opts into the
    file+dir fsync durability guarantee. ``permissions`` sets the final file mode
    (default: the umask-derived mode ``open`` would give).

    Raises the underlying ``OSError`` on failure (after removing the temp); callers
    that treat the write as best-effort keep their own ``try/except`` around the call,
    exactly as before.
    """
    path = os.fspath(path)
    binary = "b" in mode
    if binary and not isinstance(data, (bytes, bytearray)):
        raise TypeError("atomic_write(mode='wb') requires bytes data")
    if not binary and not isinstance(data, str):
        raise TypeError("atomic_write(mode='w') requires str data")

    directory = os.path.dirname(path) or "."
    base = os.path.basename(path)
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=f".{base}.", suffix=".tmp")
    try:
        if binary:
            with os.fdopen(fd, "wb") as fh:
                fh.write(data)  # type: ignore[arg-type]
                if fsync:
                    fh.flush()
                    os.fsync(fh.fileno())
        else:
            # newline="" — no newline translation, so the on-disk bytes equal `data`.
            with os.fdopen(fd, "w", encoding=encoding, newline="") as fh:
                fh.write(data)  # type: ignore[arg-type]
                if fsync:
                    fh.flush()
                    os.fsync(fh.fileno())
        os.chmod(tmp, permissions if permissions is not None else _umask_mode())
        os.replace(tmp, path)  # atomic on the same filesystem (same dir = same fs)
    except BaseException:
        # The publish never happened → drop the temp and re-raise (incl. Keyboard-
        # Interrupt / SystemExit): the target is left untouched, never half-written.
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    if fsync:
        _fsync_dir(directory)


def _fsync_dir(directory: str) -> None:
    """fsync a directory so a just-published rename is itself durable (best-effort —
    some platforms disallow opening a directory for fsync)."""
    try:
        dir_fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(dir_fd)
    except OSError:
        pass
    finally:
        os.close(dir_fd)
