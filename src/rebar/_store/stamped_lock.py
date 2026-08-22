"""One stamped-file advisory lock for the background workers (story 1cf6-f902-5bfa-438f).

The enrichment drain, the compaction sweep and the snapshot-GC worker each need the same cheap
"only one of me at a time" file lock, and each carried its OWN copy of the loop — which is how
the unresolved-path defect spread (bug ``da68-fc7c-068c-4c53``), how the three dialects drifted
apart (bug ``aadc-9af6-0e67-4e2a``), and how the release delta :func:`release_stamped_lock` now
settles survived in one of them. Deliberately narrow: staleness stays in
:mod:`rebar._store.lock_owner` (this module ASKS
:func:`~rebar._store.lock_owner.stamped_file_is_stale`, inheriting its pid-recycle
qualification, refuse-without-proof branches and single wall-clock ceiling for all three), and
directory creation stays with the three callers, who each do it differently — so this takes an
already-resolved path. These are NOT store write locks: they only stop two PROCESSES doing
redundant work, so every leg is best-effort and none may fail its caller.
"""

from __future__ import annotations

import logging
import os
import re
from collections.abc import Callable

logger = logging.getLogger(__name__)

#: The escape hatch for a legitimate fourth acquire (cf. ``# store-path-ok:``): visible only.
_MARKER_RE = re.compile(r"#\s*stamped-lock-ok:(.*)")


def stamped_file_lock(
    path: str | os.PathLike[str],
    *,
    label: str,
    lock_noun: str = "worker lock",
    describe_holder: Callable[[str], str] | None = None,
) -> int | None:
    """Take the advisory lock at *path*: an open fd, or ``None`` if a live worker holds it (or
    the filesystem refused us — a background concern never fails its caller).

    The file carries the SAME v2 ownership stamp the store's mkdir write lock writes, and a
    collision is adjudicated by the SAME shared decision table; without that, a worker that died
    between acquire and release leaked it forever and every later trigger silently skipped (bug
    ``knavish-stimulated-bluebottle``). An orphan is reclaimed LOUDLY and the create retried
    EXACTLY ONCE; a holder the table will not condemn is respected. *label* and *lock_noun* name
    the caller and the lock in the log lines; *describe_holder*, when given, renders the holder
    into the reclaim WARNING so a wedge is attributable without ``rebar doctor``. Exclusion is
    DEFEASIBLE ACROSS THE RECLAIM — see below."""
    from rebar._store import lock_owner as _owner

    target = os.fspath(path)
    for attempt in range(2):
        try:
            fd = os.open(target, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            if attempt or not _owner.stamped_file_is_stale(target):
                return None
            held = "" if describe_holder is None else f" held by {describe_holder(target)}"
            logger.warning("%s: reclaiming stale %s %s%s", label, lock_noun, target, held)
            # ACCEPTED RACE (task deathful-lettered-maltesedog, operator decision
            # 2026-08-16). Nothing excludes another drainer between the
            # `stamped_file_is_stale` verdict above and this unlink: both can condemn the
            # same orphan, the winner can recreate and stamp a FRESH lock, and this unlink
            # then removes that live lock — so two drains can run at once. (The mkdir leg
            # has the same shape but is safe, because `_acquire_mkdir`'s precondition is
            # that its caller already holds the exclusive fcntl leg; a single FILE lock has
            # no such kernel leg to inherit.) The window is left open deliberately:
            #   * Harm is bounded to REDUNDANT WORK, never to a wrong outcome. This lock is
            #     advisory and exists only to stop two drain PROCESSES duplicating effort;
            #     the correctness boundary is the per-ticket optimistic queue claim
            #     (`overlap.queue.claim`, lease-bounded), which a second drainer loses.
            #   * Closing it means changing the MECHANISM — a kernel leg (`flock`) or an
            #     atomic create-and-rename — which is a larger change than the bug it would
            #     prevent, on a path that is already strictly better than the permanent
            #     wedge it replaced (bug knavish-stimulated-bluebottle).
            #   * It is also self-limiting: the retry above is capped at ONE, so a contended
            #     reclaim converges rather than spinning.
            # Revisit only if drain work stops being idempotent, or if the queue claim
            # ceases to be the correctness boundary — either would turn "redundant" into
            # "wrong" and make the kernel leg worth its cost.
            try:
                os.unlink(target)
            except OSError:
                return None  # someone else got there first; let them run
            continue
        except OSError:
            return None  # any other open failure: a background concern never fails a caller
        try:
            os.write(fd, _owner._owner_stamp().encode("utf-8"))
        except OSError:
            # Best-effort, like the mkdir leg's stamp write: an unstamped lock is still bounded
            # by the shared ceiling, so this cannot wedge — but say so, loudly.
            logger.warning("%s: could not stamp %s; lock is unattributable", label, target)
        return fd
    return None


def release_stamped_lock(path: str | os.PathLike[str], fd: int) -> None:
    """Drop the advisory lock: close the fd, unlink the file. Best-effort in BOTH legs, and
    they are INDEPENDENT on purpose — a failing ``os.close`` must not skip the unlink, or the
    lock outlives its holder and every later trigger skips until the shared ceiling reclaims it
    an hour later. A failing unlink leaves a stamped file that table will reclaim."""
    try:
        os.close(fd)
    except OSError:
        pass
    try:
        os.unlink(path)
    except OSError:
        pass


def _offending_source(text: str) -> str | None:
    """Why *text* re-implements the acquire above, or ``None`` if it does not.

    The anchor is the CONJUNCTION of ``os.O_CREAT`` and ``stamped_file_is_stale``: a bare
    ``O_EXCL`` scan over-matches seven legitimate lines, and ``O_EXCL`` conjoined with the table
    still flags ``_commands/doctor_locks.py`` — the diagnostic READER, which adjudicates
    staleness but deliberately opens WITHOUT ``O_CREAT``. The rule lives HERE, not in the guard
    test, so the guard is not itself a second copy of the construct it keeps singular."""
    if "os.O_CREAT" not in text or "stamped_file_is_stale" not in text:
        return None
    marker = _MARKER_RE.search(text)
    if marker is None:
        return "creates a lock file (os.O_CREAT) and adjudicates it with stamped_file_is_stale"
    if not marker.group(1).strip():
        return "'# stamped-lock-ok:' requires a reason"
    return None
