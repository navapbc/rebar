"""Module-level worker payloads for the in-process fail-open harness tests.

These are top-level functions (not closures/lambdas) so they are picklable and
importable by name — robust under BOTH the ``fork`` and ``spawn`` multiprocessing
contexts the harness may pick. Each one exercises one fail-open mode of
``run_in_worker``: clean return, a raise, a hang, and a hard crash (signal death,
standing in for a segfaulting C-extension parse).
"""

from __future__ import annotations

import multiprocessing
import os
import time


def returns_value(x: int) -> int:
    """Clean completion — the harness should report value=x*2."""
    return x * 2


def returns_kwarg(*, name: str) -> str:
    return f"hello {name}"


def process_class_name() -> str:
    """Identify the context that constructed this worker process."""
    return type(multiprocessing.current_process()).__name__


def returns_big(n: int) -> str:
    """Return a payload far larger than the OS pipe buffer (~64 KB).

    Exercises the concurrent-drain path: a join-before-read harness would deadlock
    here (the child blocks in send() waiting for the parent to read) and only
    unblock at the timeout, spuriously reporting the success as a timeout.
    """
    return "x" * n


def raises_error() -> None:
    """A normal Python raise — the harness maps this to abstain(other)."""
    raise RuntimeError("boom in worker")


def hangs_forever() -> None:
    """A hang a thread/signal timeout could not interrupt — reaped via the worker boundary."""
    while True:
        time.sleep(3600)


#: The exit status Windows gives a process killed by an access violation
#: (STATUS_ACCESS_VIOLATION, 0xC0000005), as the signed int ``os._exit`` accepts.
_WINDOWS_ACCESS_VIOLATION_EXIT = -1073741819


def hard_crash() -> None:
    """Die the way a segfaulting C-extension parse dies, on this platform.

    POSIX: SIGKILL to itself, so the worker dies with a negative exitcode WITHOUT the
    faulthandler C-traceback dump an os.abort()/SIGSEGV would spew into captured test
    output; the harness maps any signal death identically. Windows has no signal death:
    a native crash ends the process with its NTSTATUS code, which is the only thing the
    parent observes, so the worker exits with the access-violation status. Either way the
    harness must map it to abstain(parse_error) and the host must survive.
    """
    import signal

    faulthandler = __import__("faulthandler")
    faulthandler.disable()
    if not hasattr(signal, "SIGKILL"):  # Windows
        os._exit(_WINDOWS_ACCESS_VIOLATION_EXIT)
    os.kill(os.getpid(), signal.SIGKILL)
