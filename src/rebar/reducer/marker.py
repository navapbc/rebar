"""Ticket .archived marker I/O with per-ticket exclusive-lock serialization."""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager

logger = logging.getLogger(__name__)

ARCHIVE_MARKER_NAME = ".archived"
MARKER_LOCK_NAME = ".write.lock"


@contextmanager
def _marker_lock(lock_path: str) -> Iterator[None]:
    from rebar._store.lock_kernel import (
        NoExclusiveLegError,
        release_exclusive,
        take_blocking_exclusive,
    )

    with open(lock_path, "a") as lock_fd:
        locked = False
        try:
            take_blocking_exclusive(lock_fd.fileno())
            locked = True
        except NoExclusiveLegError:
            yield
            return
        try:
            yield
        finally:
            if locked:
                release_exclusive(lock_fd.fileno())


def write_marker(ticket_dir: str) -> None:
    """Create <ticket_dir>/.archived as an empty file under an exclusive lock.

    Acquires an exclusive lock on <ticket_dir>/.write.lock (created if absent),
    creates the .archived marker, then releases the lock.

    On any OSError: logs a warning to stderr and returns without raising.
    Failed marker writes must not prevent callers from proceeding.
    """
    lock_path = os.path.join(ticket_dir, MARKER_LOCK_NAME)
    marker_path = os.path.join(ticket_dir, ARCHIVE_MARKER_NAME)
    try:
        with _marker_lock(lock_path):
            # Create the marker (open with 'a' is idempotent)
            with open(marker_path, "a"):
                pass
    except OSError:
        logger.warning(
            "failed to write %s marker for %s",
            ARCHIVE_MARKER_NAME,
            ticket_dir,
            exc_info=True,
        )


def remove_marker(ticket_dir: str) -> None:
    """Remove <ticket_dir>/.archived under an exclusive lock (idempotent).

    Acquires fcntl.LOCK_EX on <ticket_dir>/.write.lock (created if absent),
    removes .archived if it exists, then releases the lock.

    On any OSError: logs a warning to stderr and returns without raising.
    """
    lock_path = os.path.join(ticket_dir, MARKER_LOCK_NAME)
    marker_path = os.path.join(ticket_dir, ARCHIVE_MARKER_NAME)
    try:
        with _marker_lock(lock_path):
            try:
                os.remove(marker_path)
            except FileNotFoundError:
                pass  # Idempotent: no error if already absent
    except OSError:
        logger.warning(
            "failed to remove %s marker for %s",
            ARCHIVE_MARKER_NAME,
            ticket_dir,
            exc_info=True,
        )


def check_marker(ticket_dir: str) -> bool:
    """Return True if <ticket_dir>/.archived exists, False otherwise.

    No locking needed — existence checks are naturally consistent.
    """
    return os.path.exists(os.path.join(ticket_dir, ARCHIVE_MARKER_NAME))
