"""The store write lock must take a REAL kernel-mediated exclusive leg, or fail loudly.

Story ``friendless-alabaster-cub`` (S1 of the Windows support-target epic). ``fcntl`` has
no Windows build. ``_store/lock.py`` guarded the *import* but not the *use*, so on Windows
every store write raised ``AttributeError: 'NoneType' object has no attribute 'flock'`` —
384 of the 510 failures in the sweep, and the reason the other classes could not even be
measured.

**The two-line "skip the fcntl leg when it is None" fix is unsafe, and these tests forbid
it.** ``_acquire_mkdir`` passes ``fcntl_held=True`` into stale-lock reclamation, and that
flag is a PROOF, not a hint: a live owner would still be holding the kernel leg, so
reaching the reclamation branch means the stamped owner is dead. Degrade to mkdir-only and
the proof becomes a lie — rebar would break a write lock held by a LIVE process, which is
silent store corruption and strictly worse than not supporting Windows at all. So what is
pinned here is not "does not raise". It is: *a kernel-mediated exclusive leg is held, or
the acquire fails loudly and never reaches the mkdir leg.*

Two layers, because they prove different things.

``test_no_leg_*`` run a child interpreter with NO exclusive leg of either kind — ``fcntl``
hidden in ``sys.modules`` and ``lock_kernel``'s ``msvcrt`` handle blanked (see
:data:`_NO_LEG` for why the two are hidden by different mechanisms, and why that keeps the
premise true on Windows as well as on POSIX). A child is required because the parent has
already imported rebar against the real primitive. They pin the degradation guard: with no
leg to take, ``acquire`` must refuse and the mkdir lock must never appear.

``test_windows_leg_*`` stub the platform primitive in-process and drive the real code.
Only ``rebar._store.lock_kernel``'s two module-level primitive handles are replaced —
``fcntl`` blanked to ``None`` and a Windows-shaped ``msvcrt`` supplied — so everything
above that seam is rebar's own, unmodified: the real ``acquire``, the real mkdir leg, the
real ownership stamp, the real ``write_lock_is_busy``. The stub's ``locking`` delegates to
the host's own ``flock``, which is what makes the assertions meaningful: the leg the
Windows branch takes is a real kernel lock, so "a second acquirer is refused" is an
observation rather than a mock's say-so. (The stub is NOT installed into ``sys.modules``:
the standard library detects Windows by importing ``msvcrt``, so a global injection would
send ``subprocess`` looking for ``_winapi``.) That ``flock`` delegation is also why they
run on POSIX HOSTS ONLY: this layer is buildable exactly where the real primitive is
missing, and on Windows the very same code paths run against the real ``msvcrt``
throughout the default suite — so skipping it there drops a scaffold, not coverage.

That simulation proves rebar's Windows BRANCH. It cannot prove the real
``msvcrt.locking``'s own guarantees, so those — cross-process visibility and
release-on-process-death — are proved separately by ``test_native_*``, which run only
where ``os.name == 'nt'``, i.e. in the epic's ``sweep (windows)`` job, and skip here.
"""

from __future__ import annotations

import errno
import os
import subprocess
import sys
import textwrap
import types

import pytest

from rebar._commands import doctor_locks as _doctor
from rebar._store import lock as _lock
from rebar._store import lock_kernel as _kernel
from rebar._store import lock_owner as _owner

pytestmark = pytest.mark.unit


# ── layer 1: no leg at all — the degradation guard ────────────────────────────────────

#: Make the child interpreter a platform with NO exclusive leg at all — on POSIX and on
#: Windows alike, so the premise these tests rest on is true on both.
#:
#: ``fcntl`` is hidden the module way (``sys.modules[...] = None`` makes ``import fcntl``
#: raise ``ImportError``, exactly as a missing module does — the idiom already used by
#: ``tests/unit/test_fcntl_import_guard.py``), which also proves rebar still IMPORTS with no
#: ``fcntl``. ``msvcrt`` cannot be hidden that way: on Windows the standard library itself
#: imports it (``subprocess`` does, at module scope), so a ``sys.modules`` entry would break
#: the interpreter rather than rebar. It is blanked at the ONE handle rebar reads instead —
#: ``lock_kernel``'s module-level primitive, the same seam the layer-2 fixture patches. On
#: POSIX that handle is already ``None``, so the assignment is a no-op and POSIX behaviour is
#: exactly what it was. ``leg_name()`` then states the premise out loud and fails the child
#: immediately if it is ever false again.
_NO_LEG = """
import sys
sys.modules["fcntl"] = None   # `import fcntl` now raises ImportError, as on Windows
from rebar._store import lock_kernel as _kernel
_kernel.msvcrt = None         # and no Windows leg either: this platform offers NOTHING
assert _kernel.leg_name() == "none", "premise broken: a leg is still available"
"""


def _run_without_any_leg(body: str, tracker: str) -> subprocess.CompletedProcess[str]:
    code = _NO_LEG + textwrap.dedent(body).replace("<TRACKER>", repr(tracker))
    return subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)


def _assert_child_ok(result: subprocess.CompletedProcess[str]) -> None:
    assert result.returncode == 0, (
        f"child failed:\n--- stdout ---\n{result.stdout}\n--- stderr ---\n{result.stderr}"
    )
    assert result.stdout.strip().splitlines()[-1] == "OK", result.stdout


def test_no_leg_acquire_refuses_loudly_and_never_takes_the_mkdir_leg(tmp_path) -> None:
    """With no exclusive leg available, ``acquire`` must raise rebar's own typed error and
    the mkdir lock must never be created.

    This is the test that goes red if anyone reintroduces the unsafe degradation: skipping
    the leg would let ``_acquire_mkdir`` run — and pass ``fcntl_held=True`` — with nothing
    held. It also pins that the failure is not a raw ``AttributeError`` leaking out of an
    unguarded ``None.flock``.
    """
    _assert_child_ok(
        _run_without_any_leg(
            """
            import os
            from rebar._store import lock as _lock
            from rebar._store import lock_kernel as _kernel

            tracker = <TRACKER>
            lock_dir = os.path.join(tracker, _lock.MKDIR_LOCK_NAME)
            try:
                _lock.acquire(tracker, timeout=1, attempts=1)
            except _kernel.NoExclusiveLegError as exc:
                assert "fcntl" in str(exc) and "msvcrt" in str(exc), str(exc)
            except AttributeError as exc:      # the production defect
                raise AssertionError(f"unguarded fcntl dereference: {exc}") from None
            else:
                raise AssertionError("acquire succeeded with NO exclusive leg held")
            assert not os.path.exists(lock_dir), (
                "the mkdir leg was taken without an exclusive leg — fcntl_held=True "
                "would be a lie and a live holder's lock could be reclaimed"
            )
            print("OK")
            """,
            str(tmp_path),
        )
    )


def test_no_leg_busy_probe_fails_open(tmp_path) -> None:
    """``write_lock_is_busy`` is ADVISORY and documented to fail OPEN. With no leg it must
    answer "not busy" rather than raise — a probe failure may only degrade to today's
    behaviour (attempt the work, let the real acquire arbitrate)."""
    _assert_child_ok(
        _run_without_any_leg(
            """
            from rebar._store import lock as _lock
            assert _lock.write_lock_is_busy(<TRACKER>) is False
            print("OK")
            """,
            str(tmp_path),
        )
    )


def test_no_leg_doctor_probe_reports_unknown(tmp_path) -> None:
    """``rebar doctor --locks`` is read-only diagnostics. With no leg it must report
    ``unknown`` — never raise, and never claim a lock is free."""
    _assert_child_ok(
        _run_without_any_leg(
            """
            import os
            from rebar._commands import doctor_locks as _doctor
            from rebar._store import lock as _lock

            path = os.path.join(<TRACKER>, _lock.WRITE_LOCK_NAME)
            open(path, "a").close()
            assert _doctor._probe_fcntl(path) == _doctor.STATE_UNKNOWN
            print("OK")
            """,
            str(tmp_path),
        )
    )


# ── layer 2: the Windows leg, driven on this POSIX host ───────────────────────────────


def _windows_shaped_msvcrt() -> types.ModuleType:
    """A stand-in for ``msvcrt`` with the real signature, the real failure mode, and real
    exclusivity (delegated to the host's ``flock``).

    ``msvcrt.locking(fd, mode, nbytes)`` locks *nbytes* from the current file position and
    raises ``OSError`` on a lock violation; the Windows CRT reports that as ``EACCES`` (or
    ``EDEADLOCK`` for the retrying modes), never ``EAGAIN``. Delegating to ``flock`` gives
    the same per-open-file-description scope Windows gives per handle, so a second
    ``os.open`` of the same path genuinely conflicts — in this process too.
    """
    import fcntl as host

    module = types.ModuleType("msvcrt")
    module.LK_LOCK, module.LK_NBLCK, module.LK_UNLCK = 1, 2, 0

    def locking(fd: int, mode: int, nbytes: int) -> None:
        if mode == module.LK_UNLCK:
            host.flock(fd, host.LOCK_UN)
            return
        try:
            host.flock(fd, host.LOCK_EX | host.LOCK_NB)
        except OSError:
            raise OSError(errno.EACCES, "Permission denied") from None

    module.locking = locking
    return module


@pytest.fixture
def windows_leg(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the leg selector see a Windows-shaped interpreter: no ``fcntl``, a ``msvcrt``.

    Patching the selector's own handles (rather than ``sys.modules``) is the seam this
    repo already uses for the same purpose — see
    ``tests/unit/store/test_git_locking_no_fcntl.py`` — and it keeps the stdlib's own
    ``import msvcrt`` platform sniff untouched.

    POSIX HOSTS ONLY, and not as a convenience: the stub delegates to the host's own
    ``flock`` (see :func:`_windows_shaped_msvcrt`) so that "a second acquirer is refused"
    is an observation rather than a mock's say-so — and ``fcntl`` is precisely what
    Windows does not have. This layer is the SIMULATION of the Windows branch, needed
    only where the real primitive is unavailable; on Windows the very same code paths run
    against the real ``msvcrt`` in the default suite, and the primitive's own guarantees
    are proved by ``test_native_*`` below. Skipping here therefore removes no coverage
    from Windows — it removes a POSIX-only scaffold that cannot be built there.
    """
    if os.name == "nt":
        pytest.skip(
            "POSIX-host simulation of the Windows leg: the stub delegates to the host's "
            "fcntl, which does not exist on Windows. The real primitive is exercised "
            "natively here and proved by test_native_* below."
        )
    monkeypatch.setattr(_kernel, "fcntl", None)
    monkeypatch.setattr(_kernel, "msvcrt", _windows_shaped_msvcrt())


def test_windows_leg_write_lock_round_trips(tmp_path, windows_leg: None) -> None:
    """A full acquire/release of the store write lock over the Windows leg."""
    tracker = str(tmp_path)
    lock_dir = os.path.join(tracker, _lock.MKDIR_LOCK_NAME)

    handle = _lock.acquire(tracker, timeout=5, attempts=1)
    assert os.path.isdir(lock_dir), "the mkdir leg was not taken"
    handle.release()
    assert not os.path.isdir(lock_dir), "the mkdir leg was not released"


def test_windows_leg_excludes_a_second_acquirer(tmp_path, windows_leg: None) -> None:
    """THE contract, and what keeps ``fcntl_held=True`` honest on the Windows leg: while
    the lock is held, a second acquirer is REFUSED by a kernel lock — not merely by the
    mkdir directory. A leg-skipping degradation still creates and removes that directory,
    so the round-trip test above would not catch it; this one does."""
    tracker = str(tmp_path)
    handle = _lock.acquire(tracker, timeout=1, attempts=1)
    try:
        with pytest.raises(_lock.LockTimeout):
            _lock.acquire(tracker, timeout=1, attempts=1)
    finally:
        handle.release()

    # Reusable once released — the leg really was given back, not leaked.
    _lock.acquire(tracker, timeout=1, attempts=1).release()


def test_windows_leg_exclusion_survives_the_mkdir_leg_being_removed(
    tmp_path, windows_leg: None
) -> None:
    """The exclusive leg alone must exclude, with the mkdir leg out of the picture.

    ``dual_window=False`` is the documented fcntl-only escape hatch, so it isolates the
    kernel leg: if the Windows branch were a no-op, both acquires would succeed here and
    the "no other Python acquirer is between mkdir and release" premise would be void.
    """
    tracker = str(tmp_path)
    handle = _lock.acquire(tracker, timeout=1, attempts=1, dual_window=False)
    try:
        with pytest.raises(_lock.LockTimeout):
            _lock.acquire(tracker, timeout=1, attempts=1, dual_window=False)
    finally:
        handle.release()


def test_windows_leg_busy_probe_answers_truthfully(tmp_path, windows_leg: None) -> None:
    """``write_lock_is_busy`` (``lock.py:254``) is a second use site on a different path.
    It must answer, and answer correctly, over the Windows leg."""
    tracker = str(tmp_path)
    assert _lock.write_lock_is_busy(tracker) is False, "free store reported busy"
    handle = _lock.acquire(tracker, timeout=1, attempts=1)
    try:
        assert _lock.write_lock_is_busy(tracker) is True, "held store reported free"
    finally:
        handle.release()
    assert _lock.write_lock_is_busy(tracker) is False, "released store reported busy"


def test_windows_leg_doctor_probe_answers_truthfully(tmp_path, windows_leg: None) -> None:
    """``doctor_locks._probe_fcntl`` is the third use site on the SAME
    ``.ticket-write.lock``, reached by ``rebar doctor --locks``."""
    tracker = str(tmp_path)
    lock_path = os.path.join(tracker, _lock.WRITE_LOCK_NAME)

    handle = _lock.acquire(tracker, timeout=1, attempts=1)
    try:
        assert _doctor._probe_fcntl(lock_path) == _doctor.STATE_HELD
    finally:
        handle.release()
    assert _doctor._probe_fcntl(lock_path) == _doctor.STATE_FREE


# ── the liveness probe, narrowed where signal 0 is not a probe ────────────────────────


def test_pid_probe_never_signals_where_signal_zero_terminates(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``os.kill(pid, 0)`` is a liveness PROBE only on POSIX. On Windows ``os.kill``
    understands just the two console-control events and TerminateProcess-es the target for
    any other value, 0 included — so the probe would KILL the write-lock holder it was
    asking about, on every mkdir-lock contention.

    The contract: where signal 0 is not a probe, nothing is signalled at all.
    """
    monkeypatch.setattr(_owner, "_signal_zero_probes_liveness", lambda: False)

    def _forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("os.kill was called on a platform where it would terminate")

    monkeypatch.setattr(os, "kill", _forbidden)

    assert _owner._pid_alive(os.getpid()) is True, (
        "with no liveness signal available the answer must be the conservative one"
    )


def test_narrowed_pid_probe_does_not_reclaim_a_fresh_lock_without_proof(tmp_path) -> None:
    """Narrowing the probe must not turn into reclaiming MORE. A fresh mkdir lock with a
    same-host stamp and no exclusive-leg proof stays refused, exactly as before."""
    lock_dir = tmp_path / _lock.MKDIR_LOCK_NAME
    lock_dir.mkdir()
    (lock_dir / _owner._MKDIR_OWNER_FILE).write_text(_owner._owner_stamp(), encoding="utf-8")

    assert _owner._mkdir_lock_is_stale(str(lock_dir), fcntl_held=False) is False


# ── layer 3: the real Windows primitive, proved only where it exists ──────────────────
#
# The simulation above proves rebar's Windows BRANCH; it cannot prove msvcrt.locking's own
# guarantees, because the stub delegates to flock. These two do, and they are the reason
# the `fcntl_held=True` proof is allowed to stand on Windows. They run in the epic's
# `sweep (windows)` job and skip everywhere else.

_needs_windows = pytest.mark.skipif(
    os.name != "nt", reason="proves the real msvcrt.locking primitive; Windows only"
)

_CHILD_HOLDS_THE_LOCK = """
import sys, time
from rebar._store import lock as _lock
handle = _lock.acquire(sys.argv[1], timeout=10, attempts=1)
print("HELD", flush=True)
time.sleep(600)
"""


def _spawn_holder(tracker: str) -> subprocess.Popen[str]:
    """Start a child that takes the write lock and holds it. Caller MUST reap it."""
    proc = subprocess.Popen(
        [sys.executable, "-c", _CHILD_HOLDS_THE_LOCK, tracker],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert proc.stdout is not None
    assert proc.stdout.readline().strip() == "HELD", "child never took the lock"
    return proc


@_needs_windows
def test_native_lock_is_visible_across_processes(tmp_path) -> None:
    """Property 1 of the Windows proof: the lock lives on the kernel's file object, so a
    DIFFERENT process is genuinely excluded. Without this, ``fcntl_held=True`` would prove
    nothing about any other process and reclamation could break a live holder's lock."""
    tracker = str(tmp_path)
    proc = _spawn_holder(tracker)
    try:
        assert _lock.write_lock_is_busy(tracker) is True, (
            "another process holds the lock but this process does not see it"
        )
        with pytest.raises(_lock.LockTimeout):
            _lock.acquire(tracker, timeout=2, attempts=1)
    finally:
        proc.kill()
        proc.wait(timeout=30)


@_needs_windows
def test_native_lock_dies_with_its_holder(tmp_path) -> None:
    """Property 2: the kernel releases the lock when the owning handle closes, which it
    does for every handle when the process terminates — abnormally included. This is the
    property the staleness argument actually rests on, and it is proved end to end: the
    holder is killed without releasing, so its mkdir lock and ownership stamp are left
    behind, and a fresh acquire must still succeed by taking the exclusive leg and
    reclaiming the orphaned mkdir lock under that proof."""
    tracker = str(tmp_path)
    lock_dir = os.path.join(tracker, _lock.MKDIR_LOCK_NAME)
    proc = _spawn_holder(tracker)
    try:
        proc.kill()
        proc.wait(timeout=30)
    finally:
        if proc.poll() is None:  # pragma: no cover - the kill above normally settles it
            proc.kill()
            proc.wait(timeout=30)

    assert os.path.isdir(lock_dir), "the killed holder should have left its mkdir lock"
    handle = _lock.acquire(tracker, timeout=15, attempts=1)
    handle.release()
