"""Share a best-effort advisory file lock among background workers.

Callers supply resolved paths and create their own directories. Shared lock-owner
logic decides staleness, including pid reuse and its age ceiling. These locks limit
duplicate worker processes; store write locks still protect correctness.
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
    """Acquire a stamped advisory fd, or return ``None`` without failing the caller.

    Respect a live or uncertain holder. Log and reclaim a proven orphan with one
    retry; caller labels and optional holder details identify that reclaim.
    """
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
            # Accepted stale-unlink race: another worker may replace the orphan
            # before this unlink, allowing 2 workers. Their actual work still takes
            # the store lock; the one retry bounds duplicate effort. Revisit if that
            # work stops being idempotent or the store lock stops guarding correctness.
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
    """Independently close and unlink, so either failure cannot skip the other leg.

    A leftover stamped file remains reclaimable under the shared age ceiling.
    """
    try:
        os.close(fd)
    except OSError:
        pass
    try:
        os.unlink(path)
    except OSError:
        pass


def _offending_source(text: str) -> str | None:
    """Flag another stamped-lock acquire, requiring both create and stale checks.

    The conjunction excludes diagnostic readers and legitimate ``O_EXCL`` uses.
    """
    if "os.O_CREAT" not in text or "stamped_file_is_stale" not in text:
        return None
    marker = _MARKER_RE.search(text)
    if marker is None:
        return "creates a lock file (os.O_CREAT) and adjudicates it with stamped_file_is_stale"
    if not marker.group(1).strip():
        return "'# stamped-lock-ok:' requires a reason"
    return None
