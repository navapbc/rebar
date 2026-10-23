"""Publish a new ticket directory and its first event with one atomic rename.

Build both in a hidden ``.tmp-newticket-<pid>-<uuid>`` directory. The dot hides
staging from store scanners; pid and UUID prevent writer collisions. An interrupted
write strands only reclaimable staging, never an empty visible ticket directory.
"""

from __future__ import annotations

import errno
import os
import shutil
import tempfile
import time
import uuid as _uuid
from dataclasses import dataclass

from rebar._store import lock_owner as _owner

# The staging-path prefix. Dot-prefixed on purpose — see the module docstring.
STAGING_PREFIX = ".tmp-newticket-"

# Stamp staging ownership with host, pid namespace, and process start time;
# remove it before publication so pid reuse cannot fool the sweep.
_OWNER_FILE = ".rebar-staging-owner"

# Reclaim an unprobeable owner only after this generous age ceiling.
_UNPROBEABLE_STALE_S = 6 * 60 * 60

# Bound best-effort write-path cleanup by scanned and removed entries.
_SWEEP_SCAN_LIMIT = 512
_SWEEP_REMOVE_LIMIT = 32


def _silent_unlink(path: str) -> None:
    try:
        os.unlink(path)
    except OSError:
        pass


def _new_staging_path(tracker: str) -> str:
    """A unique, scanner-invisible staging path for *tracker* (not created)."""
    return os.path.join(tracker, f"{STAGING_PREFIX}{os.getpid()}-{_uuid.uuid4().hex[:12]}")


def _write_owner_stamp(staging_dir: str, *, stamp: str | None = None) -> None:
    """Record who owns *staging_dir*. Best-effort: a failed stamp only costs the sweep an
    early reclaim (the age fallback still applies), never correctness of this write."""
    try:
        with open(os.path.join(staging_dir, _OWNER_FILE), "w", encoding="utf-8") as fh:
            fh.write(stamp if stamp is not None else _owner._owner_stamp())
    except OSError:
        pass


def _read_owner_stamp(staging_dir: str) -> str | None:
    try:
        with open(os.path.join(staging_dir, _OWNER_FILE), encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return None


def _age_s(path: str) -> float | None:
    try:
        return time.time() - os.stat(path).st_mtime
    except OSError:
        return None


def _is_abandoned(staging_dir: str) -> bool:
    """Keep uncertain or live owners; reclaim only a dead or sufficiently old one."""
    stamp = _read_owner_stamp(staging_dir)
    if stamp:
        fields = _owner._parse_v2_stamp(stamp)
        if fields:
            verdict = _owner._describe_stamped_pid(fields)
            if verdict == "live":
                return False
            if verdict == "not-running" or verdict.startswith("not-owner"):
                return True
        # A torn or foreign stamp carries no verdict: fall through to the age ceiling.
    age = _age_s(staging_dir)
    return age is not None and age > _UNPROBEABLE_STALE_S


def sweep_stale_staging(tracker: str) -> int:
    """Best-effort, bounded cleanup of abandoned hidden staging; return removals.

    Only hidden staging with dead owners or unprobeable paths past the age
    ceiling is swept. Visible ticket directories, including event-less ones, stay.
    """
    removed = 0
    try:
        names = os.listdir(tracker)
    except OSError:
        return 0
    for name in sorted(names)[:_SWEEP_SCAN_LIMIT]:
        if removed >= _SWEEP_REMOVE_LIMIT:
            break
        if not name.startswith(STAGING_PREFIX):
            continue
        path = os.path.join(tracker, name)
        if not os.path.isdir(path) or not _is_abandoned(path):
            continue
        shutil.rmtree(path, ignore_errors=True)
        if not os.path.exists(path):
            removed += 1
    return removed


@dataclass
class StagedEvent:
    """Publish a new ticket by directory rename, or an existing ticket by file replace.

    ``staging_dir`` distinguishes the new-ticket path from a staged ``.tmp-event-*``.
    """

    staging: str
    final_path: str
    relative_path: str
    ticket_dir: str
    staging_dir: str | None = None
    published_dir: bool = False

    def promote(self) -> None:
        """Publish under the write lock, after rebase and concurrency checks pass."""
        if self.staging_dir is None:
            os.replace(self.staging, self.final_path)
            return
        # Never publish our own scratch stamp into the ticket directory.
        _silent_unlink(os.path.join(self.staging_dir, _OWNER_FILE))
        try:
            os.rename(self.staging_dir, self.ticket_dir)
        except OSError as exc:
            if exc.errno not in (errno.ENOTEMPTY, errno.EEXIST):
                raise
            # A concurrent create published this ticket's directory first. Fall back to
            # publishing just our event file into it — the same outcome, one step later.
            os.replace(self.staging, self.final_path)
            self.discard()
            return
        self.published_dir = True

    def unpublish(self) -> None:
        """Remove only this transaction's empty published directory under the lock.

        The event must already be rolled back, so another writer's content survives.
        """
        if not self.published_dir:
            return
        try:
            os.rmdir(self.ticket_dir)
        except OSError:
            return
        self.published_dir = False

    def discard(self) -> None:
        """Abandon the staged event. A no-op once published; never raises."""
        if self.staging_dir is None:
            _silent_unlink(self.staging)
            return
        shutil.rmtree(self.staging_dir, ignore_errors=True)


def stage_event(
    tracker: str,
    ticket_id: str,
    filename: str,
    payload: bytes,
    *,
    sweep_stale: bool = True,
) -> StagedEvent:
    """Stage an event out of sight for atomic publication under the later lock.

    New tickets do not touch their visible directory; staging failures raise
    ``OSError`` without publishing partial state.
    """
    ticket_dir = os.path.join(tracker, ticket_id)
    final_path = os.path.join(ticket_dir, filename)
    relative_path = os.path.relpath(final_path, tracker)

    if os.path.isdir(ticket_dir):
        # The directory already exists, so there is no dir-then-first-file window to close;
        # this is the historical file-staging path, byte-for-byte unchanged.
        fd, staging = tempfile.mkstemp(prefix=".tmp-event-", dir=tracker)
        try:
            with os.fdopen(fd, "wb") as fh:
                fh.write(payload)
        except OSError:
            _silent_unlink(staging)
            raise
        return StagedEvent(staging, final_path, relative_path, ticket_dir)

    # New-ticket writes create staging paths, so sweep abandoned ones here.
    if sweep_stale:
        sweep_stale_staging(tracker)

    # Reconciler writes may need the tracker root; only an empty ticket directory
    # would be visible debris.
    os.makedirs(tracker, exist_ok=True)
    staging_dir = _new_staging_path(tracker)
    os.mkdir(staging_dir)
    staged = os.path.join(staging_dir, filename)
    try:
        with open(staged, "wb") as fh:
            fh.write(payload)
    except OSError:
        shutil.rmtree(staging_dir, ignore_errors=True)
        raise
    _write_owner_stamp(staging_dir)
    return StagedEvent(staged, final_path, relative_path, ticket_dir, staging_dir)
