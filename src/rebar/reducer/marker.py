"""Ticket .archived marker I/O with per-ticket fcntl.flock serialization."""

from __future__ import annotations

import fcntl
import os
import sys


def write_marker(ticket_dir: str) -> None:
    """Create <ticket_dir>/.archived as an empty file under an exclusive lock.

    Acquires fcntl.LOCK_EX on <ticket_dir>/.write.lock (created if absent),
    creates the .archived marker, then releases the lock.

    On any OSError: logs a warning to stderr and returns without raising.
    Failed marker writes must not prevent callers from proceeding.
    """
    lock_path = os.path.join(ticket_dir, ".write.lock")
    marker_path = os.path.join(ticket_dir, ".archived")
    try:
        with open(lock_path, "a") as lock_fd:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
                # Create the marker (open with 'a' is idempotent)
                with open(marker_path, "a"):
                    pass
            finally:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
    except OSError as exc:
        print(
            f"WARNING: failed to write .archived marker for {ticket_dir}: {exc}",
            file=sys.stderr,
        )


def remove_marker(ticket_dir: str) -> None:
    """Remove <ticket_dir>/.archived under an exclusive lock (idempotent).

    Acquires fcntl.LOCK_EX on <ticket_dir>/.write.lock (created if absent),
    removes .archived if it exists, then releases the lock.

    On any OSError: logs a warning to stderr and returns without raising.
    """
    lock_path = os.path.join(ticket_dir, ".write.lock")
    marker_path = os.path.join(ticket_dir, ".archived")
    try:
        with open(lock_path, "a") as lock_fd:
            try:
                fcntl.flock(lock_fd, fcntl.LOCK_EX)
                try:
                    os.remove(marker_path)
                except FileNotFoundError:
                    pass  # Idempotent: no error if already absent
            finally:
                fcntl.flock(lock_fd, fcntl.LOCK_UN)
    except OSError as exc:
        print(
            f"WARNING: failed to remove .archived marker for {ticket_dir}: {exc}",
            file=sys.stderr,
        )


def check_marker(ticket_dir: str) -> bool:
    """Return True if <ticket_dir>/.archived exists, False otherwise.

    No locking needed — existence checks are naturally consistent.
    """
    return os.path.exists(os.path.join(ticket_dir, ".archived"))
