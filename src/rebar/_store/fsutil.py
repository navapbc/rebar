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
import stat
import tempfile
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import IO, Any, TypeVar

_T = TypeVar("_T")

__all__ = [
    "atomic_write",
    "open_published",
    "published_is_file",
    "replace_with_retry",
    "retry_contended",
    "sibling_exclusive_lock",
]

#: Windows ``os.replace`` is ``MoveFileEx(MOVEFILE_REPLACE_EXISTING)``, which fails with
#: ``ERROR_ACCESS_DENIED`` (``PermissionError``) while another rename onto the same target
#: is in flight or another handle holds it open. POSIX ``rename(2)`` never fails that way,
#: so the retry below runs only here (bug sporting-feudalist-bluet).
_RETRY_CONTENDED_REPLACE = os.name == "nt"
#: How long a contended Windows replace keeps retrying before the error surfaces. The
#: competing rename or handle finishes within milliseconds; the window only has to outlast it.
_REPLACE_RETRY_DEADLINE_S = 2.0
_REPLACE_RETRY_FIRST_SLEEP_S = 0.01
_REPLACE_RETRY_MAX_SLEEP_S = 0.1


def retry_contended(fn: Callable[..., _T], /, *args: Any, **kwargs: Any) -> _T:
    """Call ``fn(*args, **kwargs)``, retrying a transient Windows ``PermissionError``.

    While a ``MoveFileEx`` replace onto a path is in flight, Windows refuses a second
    replace onto it, and an open or stat of it, with ``ERROR_ACCESS_DENIED``. On Windows a
    ``PermissionError`` is retried with a short doubling backoff until
    :data:`_REPLACE_RETRY_DEADLINE_S` has passed, then the last error is re-raised. On
    every other platform ``fn`` runs once. Any other exception propagates at once
    everywhere (bugs sporting-feudalist-bluet, ruby-unwomanish-willet).
    """
    if not _RETRY_CONTENDED_REPLACE:
        return fn(*args, **kwargs)
    deadline = time.monotonic() + _REPLACE_RETRY_DEADLINE_S
    sleep_s = _REPLACE_RETRY_FIRST_SLEEP_S
    while True:
        try:
            return fn(*args, **kwargs)
        except PermissionError:
            if time.monotonic() >= deadline:
                raise
        time.sleep(sleep_s)
        sleep_s = min(sleep_s * 2, _REPLACE_RETRY_MAX_SLEEP_S)


def replace_with_retry(src: str | os.PathLike[str], dst: str | os.PathLike[str]) -> None:
    """``os.replace(src, dst)`` through :func:`retry_contended`."""
    retry_contended(os.replace, src, dst)


def open_published(
    path: str | os.PathLike[str], mode: str = "r", *, encoding: str = "utf-8"
) -> IO[Any]:
    """Open a file another process may be republishing with :func:`atomic_write`.

    The refusal happens at open time, so only the open is retried. ``encoding`` applies
    to text modes and is ignored for binary ones. ``FileNotFoundError`` propagates, so
    callers decide absence from the open itself rather than from a separate stat.
    """
    if "b" in mode:
        return retry_contended(open, path, mode)
    return retry_contended(open, path, mode, encoding=encoding)


def published_is_file(path: str | os.PathLike[str]) -> bool:
    """``os.path.isfile`` for a published file, with the stat retried like an open.

    A missing path (or one under a non-directory) is False; a refusal that outlasts the
    retry deadline propagates instead of reading as absent.
    """
    try:
        return stat.S_ISREG(retry_contended(os.stat, path).st_mode)
    except (FileNotFoundError, NotADirectoryError):
        return False


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
        replace_with_retry(tmp, path)  # atomic on the same filesystem (same dir = same fs)
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
