"""THE kernel-mediated exclusive leg of the store write lock, one per platform.

Every store write-lock probe goes through here: :func:`rebar._store.lock._acquire_fcntl`
(the acquire path), :func:`rebar._store.lock.write_lock_is_busy` (the advisory
stand-aside probe) and :func:`rebar._commands.doctor_locks._probe_fcntl` (``rebar doctor
--locks``). They previously each called ``fcntl.flock`` directly. ``fcntl`` has no Windows
build, and while ``lock.py`` guarded the *import* (``except ImportError: fcntl = None``)
nothing guarded the *uses*, so on Windows every ticket write raised ``AttributeError:
'NoneType' object has no attribute 'flock'`` — 384 of the 510 failures in the Windows
sweep (story ``friendless-alabaster-cub``).

**Why this is a leg selector and not an ``if fcntl is None: skip``.** The mkdir leg's
reclamation logic consumes ``fcntl_held=True`` from :func:`rebar._store.lock._acquire_mkdir`,
and that flag is a PROOF of a dead owner, not a hint: a live owner would still be holding
this leg, so reaching the reclamation branch means the stamped owner is gone. Skipping the
leg would leave that argument asserting something nobody holds, and rebar would break a
write lock owned by a LIVE process — silent store corruption, strictly worse than not
running on Windows at all, because the failure is invisible instead of loud. So the
contract here is total: a real kernel-mediated leg is taken, or :class:`NoExclusiveLegError`
is raised and the caller never reaches the mkdir leg.

**The POSIX leg is unchanged.** :func:`take_exclusive` on POSIX is exactly
``fcntl.flock(fd, LOCK_EX | LOCK_NB)`` and :func:`is_contention` is exactly
``errno in (EAGAIN, EACCES)``, the discrimination the acquire path has always used (only
genuine contention is waited out; ENOLCK/EIO/EBADF must surface with their own identity
rather than be masked as a spurious LockTimeout).

**The Windows leg, and its own staleness proof.** ``msvcrt.locking(fd, LK_NBLCK, 1)`` is
the CRT wrapper over ``LockFile``. The POSIX proof — "``flock`` is kernel-mediated on the
inode, so it is shared across pid/mount namespaces on ONE kernel and the kernel drops it
when its holder dies" — does not transfer as written, because Windows has neither inodes
nor pid namespaces. The Windows proof is its own argument, and it rests on two properties
of the Win32 file-locking API:

1. **Byte-range locks live on the file object in the kernel, not in the process.** A lock
   taken through one handle is enforced against every other handle on that file,
   process-wide and machine-wide, so a second acquirer of ``.ticket-write.lock`` is
   genuinely refused. (This is *stronger* than POSIX ``flock``, which is advisory; Windows
   byte-range locks are mandatory.)
2. **The kernel releases every lock when the owning handle closes, and closes every handle
   when the process terminates** — including abnormal termination, since handle teardown
   is done by the kernel and not by the CRT. So the lock dies with its holder, which is
   the property the staleness proof actually needs.

Together those give the same conclusion the POSIX branch reaches by a different route: if
we hold this leg, no live process holds the write lock, so a same-host mkdir ownership
stamp we find behind it belongs to a dead owner. ``fcntl_held=True`` therefore stays TRUE
under the Windows leg; the parameter keeps its POSIX-era name and now denotes the
platform's exclusive leg generally (see :func:`rebar._store.lock._acquire_mkdir`).

Neither property is provable from a POSIX host, so they are not asserted and left there:
``tests/unit/store/test_windows_exclusive_leg.py`` proves them empirically with
``test_native_*``, which run only where ``os.name == 'nt'`` — i.e. in the Windows sweep.

**Release is by closing the fd**, on both legs and for the same reason: that is the
mechanism property 2 above depends on. :func:`release_exclusive` exists only for the one
caller that already unlocked explicitly before closing (the doctor probe), so its shape is
preserved rather than quietly changed.
"""

from __future__ import annotations

import errno
import os

try:  # POSIX advisory locking; absent on some platforms (e.g. plain Windows)
    import fcntl
except ImportError:  # pragma: no cover - platform-dependent
    fcntl = None  # type: ignore[assignment]

try:  # the Windows CRT locking API; absent everywhere else
    import msvcrt
except ImportError:  # pragma: no cover - platform-dependent
    msvcrt = None  # type: ignore[assignment]

__all__ = [
    "NoExclusiveLegError",
    "is_contention",
    "leg_name",
    "release_exclusive",
    "take_exclusive",
]

#: The errnos that mean "another holder has it" rather than "the call itself failed".
#: POSIX ``flock`` reports contention as EAGAIN (EWOULDBLOCK) or EACCES. The Windows CRT
#: reports a lock violation as EACCES for the non-blocking modes and EDEADLOCK for the
#: retrying ones; EDEADLOCK is spelled ``EDEADLK`` in Python's ``errno``. Everything else
#: is a real fault that must surface with its own identity.
_POSIX_CONTENTION = frozenset({errno.EAGAIN, errno.EACCES})
_WINDOWS_CONTENTION = frozenset({errno.EACCES, errno.EAGAIN, errno.EDEADLK})

#: Windows byte-range locks need a non-empty range; the lock file itself is empty, and
#: locking past EOF is legal, so one byte at offset 0 is the whole rendezvous.
_WINDOWS_LOCK_BYTES = 1


class NoExclusiveLegError(RuntimeError):
    """No kernel-mediated exclusive lock primitive exists on this platform.

    Raised rather than degrading, because a degraded acquire would make
    ``fcntl_held=True`` a lie and let rebar reclaim a live process's write lock. Callers
    whose contract is ADVISORY (the busy probe, the doctor probe) catch it and report
    "unknown"/"not busy"; the acquire path lets it propagate.
    """


def leg_name() -> str:
    """The primitive backing the exclusive leg here, for diagnostics."""
    if fcntl is not None:
        return "fcntl.flock"
    if msvcrt is not None:
        return "msvcrt.locking"
    return "none"


def take_exclusive(fd: int) -> None:
    """Take the exclusive lock on *fd*, without waiting.

    Returns on success. Raises :class:`OSError` if the lock could not be taken — pass it
    to :func:`is_contention` to tell "someone else holds it" from a real fault — and
    :class:`NoExclusiveLegError` if the platform offers no primitive at all.
    """
    if fcntl is not None:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        return
    if msvcrt is not None:
        # ``locking`` acts on the range starting at the CURRENT position, so anchor it.
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, _WINDOWS_LOCK_BYTES)
        return
    raise NoExclusiveLegError(
        "no kernel-mediated exclusive lock primitive on this platform: neither fcntl "
        "(POSIX) nor msvcrt (Windows) is importable, so the store write lock cannot be "
        "held safely and rebar refuses to take it rather than degrade to an unproven one"
    )


def release_exclusive(fd: int) -> None:
    """Explicitly drop the exclusive lock on *fd*.

    Closing the fd releases it on both platforms; this exists for the caller that
    already unlocked explicitly before closing, so that shape is preserved. Raises
    :class:`OSError` on failure and :class:`NoExclusiveLegError` when there is no leg.
    """
    if fcntl is not None:
        fcntl.flock(fd, fcntl.LOCK_UN)
        return
    if msvcrt is not None:
        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, _WINDOWS_LOCK_BYTES)
        return
    raise NoExclusiveLegError("no exclusive lock primitive to release")


def is_contention(exc: OSError) -> bool:
    """Whether *exc* from :func:`take_exclusive` means another holder has the lock.

    False for every other errno — those are real faults (ENOLCK/EIO/EBADF/…) that must
    surface with their identity rather than be waited out as contention.
    """
    if fcntl is not None:
        return exc.errno in _POSIX_CONTENTION
    return exc.errno in _WINDOWS_CONTENTION
