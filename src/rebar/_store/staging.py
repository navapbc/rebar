"""Atomic publication of a NEW ticket directory: stage out of sight, then one rename.

The problem this closes
-----------------------
The write path used to ``os.makedirs`` the ticket directory and only land its first event
much later, at the under-lock ``os.replace`` — with the temp write, the write-lock
acquisition and the rebase guard in between. Process death anywhere in that window (host
sleep, session kill, shell timeout) left an empty, plausible-looking ticket directory in
the tracker worktree, which ``fsck`` reports TWICE: ``MISSING_CREATE`` (the reducer finds
no CREATE event) and ``FOREIGN_STORE_PATH`` (the directory holds no event file). Eight such
directories were swept by hand under ticket illsuited-erect-ibis before this fix existed.

Here the directory and its first event are built together in a staging path and published
by a single :func:`os.rename`, which is atomic on one filesystem. An interruption can
therefore only ever strand the staging path — never a bare ticket directory.

The naming convention (this is the load-bearing part)
-----------------------------------------------------
A staging path is ``<tracker>/.tmp-newticket-<pid>-<uuid4hex>``:

* The **leading dot** makes it invisible to every store scanner, by a rule that already
  existed rather than one added for this module — ``fsck._ticket_dirs`` and
  ``fsck.foreign_store_path_list`` both skip top-level entries starting with ``.``, as do
  fsck's JSON check and the plan reducer's ``relation_snapshot``. Ticket ids never start
  with a dot, so the two namespaces cannot collide. This is the same convention the
  pre-existing ``.tmp-event-*`` event staging files rely on.
* The **pid and uuid4** make the name unique per writer and per call, so concurrent creates
  of different tickets cannot collide on a staging path (and a retry never reuses one).

Anything stranded here is the writer's own scratch, not store data — which is what lets the
sweep below reclaim it without contradicting bug 043f.
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

# Ownership stamp INSIDE a staging directory, reusing the write lock's v2 stamp format
# (host + pid namespace + process start time). Start time is what makes the probe safe
# against pid recycling. Removed just before publication so it is never renamed into the
# published ticket directory.
_OWNER_FILE = ".rebar-staging-owner"

# Fallback age ceiling for a staging path whose owner cannot be probed (no stamp, a torn
# stamp, a foreign host, another pid namespace). Deliberately generous: an unprobeable
# path is only reclaimed once no plausible writer could still be working on it.
_UNPROBEABLE_STALE_S = 6 * 60 * 60

# Sweep bounds. The sweep is best-effort housekeeping on the write path, never a scan the
# store depends on, so it looks at a fixed slice of the directory and removes a fixed
# number of paths at most.
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
    """Whether *staging_dir*'s owner is provably gone.

    Fail-closed at every step: a live pid, an unreadable age, or any uncertainty keeps the
    path. Only a positive "that process is not running" verdict — or an age no live writer
    could plausibly still be inside — licenses removal."""
    stamp = _read_owner_stamp(staging_dir)
    if stamp:
        fields = _owner._parse_v2_stamp(stamp)
        if fields:
            verdict = _owner._describe_stamped_pid(fields)
            if verdict == "live":
                return False
            if verdict == "not-running":
                return True
        # A torn or foreign stamp carries no verdict: fall through to the age ceiling.
    age = _age_s(staging_dir)
    return age is not None and age > _UNPROBEABLE_STALE_S


def sweep_stale_staging(tracker: str) -> int:
    """Reclaim abandoned staging paths in *tracker*. Returns how many were removed.

    Bounded and best-effort — it never raises, and a failure to remove one path is simply
    left for the next writer.

    **This does not contradict bug 043f ("tolerate, never tidy").** That ruling forbids the
    writer from deleting STORE DATA — an event-less ticket directory — because doing so
    races another session's in-flight write in a tracker many sessions share, and because
    the reader-side tolerance also repairs clones that already carry one. Nothing here
    touches a ticket directory: this only removes ``.tmp-newticket-*`` paths, which are
    this writer's own staging area, are invisible to every reader, and are removed only
    when their owning process is provably gone. Event-less ticket directories are still
    tolerated and never tidied."""
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
    """One event staged for publication, and how to publish or abandon it.

    Two shapes, chosen by whether the ticket directory already exists:

    * **new ticket** — ``staging_dir`` is set and holds the event; :meth:`promote` renames
      the whole directory into place, so the directory and its first event become visible
      in ONE atomic step;
    * **existing ticket** — ``staging_dir`` is ``None`` and ``staging`` is a
      ``.tmp-event-*`` file beside the tracker; :meth:`promote` is the historical single
      ``os.replace``, unchanged.
    """

    staging: str
    final_path: str
    relative_path: str
    ticket_dir: str
    staging_dir: str | None = None
    published_dir: bool = False

    def promote(self) -> None:
        """Publish the event. MUST be called with the store write lock held.

        Keeping the rename under the lock is what preserves the existing discipline: an
        event file never becomes visible before the caller's under-lock checks (the rebase
        guard, and optimistic-concurrency ``under_lock_check``) have passed."""
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
        """Undo :meth:`promote`'s directory publication when the transaction rolls back.

        Only ever removes a directory THIS transaction created, only while its own event
        file has already been rolled back, and only while the write lock is still held —
        so it cannot race another session, and cannot remove a directory that holds
        anything. That keeps a failed commit from re-creating the very debris this module
        exists to prevent."""
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
    """Stage *payload* as *filename* for *ticket_id*, ready for an atomic publish.

    No lock is held here and nothing under ``<tracker>/<ticket_id>`` is touched: for a new
    ticket the directory is built out of sight and only appears at :meth:`StagedEvent
    .promote`. Raises :class:`OSError` on a staging failure, leaving no partial state."""
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

    # Creating a ticket is the only moment a staging path can appear, so it is also the
    # cheapest place to reclaim ones abandoned earlier — bounded, best-effort, and never
    # touching store data (see sweep_stale_staging).
    if sweep_stale:
        sweep_stale_staging(tracker)

    # The TRACKER root may not exist yet on the reconciler paths, which reached here via
    # ``mkdir(parents=True)``. Creating it is not the window this module closes — the debris
    # signature is an empty TICKET directory, and the tracker root holding no ticket yet is
    # both normal and invisible to the ticket scanners.
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
