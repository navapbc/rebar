"""Module-level worker payloads for the in-process fail-open harness tests.

These are top-level functions (not closures/lambdas) so they are picklable and
importable by name — robust under BOTH the ``fork`` and ``spawn`` multiprocessing
contexts the harness may pick. Each one exercises one fail-open mode of
``run_in_worker``: clean return, a raise, a hang, and a hard crash (signal death,
standing in for a segfaulting C-extension parse).
"""

from __future__ import annotations

import os
import time


def returns_value(x: int) -> int:
    """Clean completion — the harness should report value=x*2."""
    return x * 2


def returns_kwarg(*, name: str) -> str:
    return f"hello {name}"


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


def hard_crash() -> None:
    """Die on a signal, standing in for a segfaulting C-extension parse.

    Sends SIGKILL to itself so the worker dies with a negative exitcode (which the
    harness maps to abstain(parse_error)) WITHOUT the faulthandler C-traceback dump
    an os.abort()/SIGSEGV would spew into captured test output. The harness maps any
    signal death identically, so the choice of signal is immaterial to what's tested
    — a hard crash in an in-process binding must never take down the host.
    """
    import signal

    faulthandler = __import__("faulthandler")
    faulthandler.disable()
    os.kill(os.getpid(), signal.SIGKILL)
