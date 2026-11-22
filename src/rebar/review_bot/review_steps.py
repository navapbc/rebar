"""Bounded, logged review steps and the review's scratch folder (bug 688f-060e-c96c-4484).

A review runs several blocking steps on worker threads before the LLM call: the existing-vote
check, the commit fetch, the clone, the diff fetch and the snapshot build. On 2026-11-21 those
steps could block silently for the whole 1200 s review timeout (the snapshot build waited on
the snapshot store's accounting lock while the janitor's startup sweep held it). This module
gives every such step:

- one ``review_step`` INFO line BEFORE it starts, so a stall names its step in the journal;
- its own bound, :func:`step_timeout_seconds`, a quarter of the per-review timeout, so a stuck
  step fails the review as a retryable error long before the review timeout.

It also owns the review's ``reviewbot-*`` scratch folder. ``asyncio.wait_for`` cannot stop a
worker thread: when a review is abandoned the thread keeps running. A ``TemporaryDirectory``
owned by the coroutine was deleted the moment the coroutine was cancelled, so the thread later
failed with ``cannot change to '.../reviewbot-...': No such file or directory``.
:class:`ReviewScratch` removes the folder only once no step thread is running in it, and
refuses to start a step after the review was abandoned.
"""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import tempfile
import threading
import time
from collections.abc import Callable
from typing import Any, TypeVar

from rebar.review_bot.config import review_timeout_seconds

logger = logging.getLogger("rebar.review_bot.voter")

T = TypeVar("T")

#: A step's bound is the per-review timeout divided by this. At the 1200 s default that is
#: 300 s: well under the review timeout, yet far above a healthy step (seconds to ~1 minute).
#: Derived from the existing ``REVIEW_TIMEOUT_SECONDS`` setting, so it adds no new knob.
STEP_TIMEOUT_DIVISOR = 4

#: Prefix of the review's scratch folder under ``TMPDIR`` (the gate-scratch volume on the box).
SCRATCH_PREFIX = "reviewbot-"


def step_timeout_seconds() -> float:
    """Return the bound for one pre-LLM review step."""
    return review_timeout_seconds() / STEP_TIMEOUT_DIVISOR


class ReviewStepTimeout(Exception):
    """A review step exceeded :func:`step_timeout_seconds`."""

    def __init__(self, step: str, timeout: float) -> None:
        super().__init__(f"review step {step} timed out after {timeout:g}s")
        self.step = step
        self.timeout = timeout


class ReviewAbandoned(RuntimeError):
    """A step was submitted after its review was abandoned; it did not run."""


def log_step_start(step: str, ids: tuple[str, str], *, timeout: float) -> None:
    """Emit the one ``review_step`` line that marks a step's start. ``ids`` is the review's
    ``(change_id, revision_id)``."""
    record = {
        "event": "review_step",
        "timestamp": time.time(),
        "step": step,
        "change_id": ids[0],
        "revision_id": ids[1],
        "timeout_seconds": timeout,
    }
    logger.info(json.dumps(record, default=str))


class ReviewScratch:
    """The review's scratch folder, removed only after its last step thread returns.

    Use as ``with ReviewScratch() as scratch:`` and run blocking work through
    :meth:`run`. Leaving the block (normally, by error, or by cancellation) closes the
    scratch: the folder is removed at once when no step thread is running in it, otherwise
    by the last running thread as it returns."""

    def __init__(self) -> None:
        self.path = tempfile.mkdtemp(prefix=SCRATCH_PREFIX)
        # mechanism-ok: lock ReviewScratch._lock — folder outlives its last step, 688f-060e-c96c
        self._lock = threading.Lock()
        self._running = 0
        self._closed = False

    def __enter__(self) -> ReviewScratch:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def close(self) -> None:
        with self._lock:
            self._closed = True
            remove = self._running == 0
        if remove:
            self._remove()

    def _remove(self) -> None:
        shutil.rmtree(self.path, ignore_errors=True)

    def _call(self, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        with self._lock:
            if self._closed:
                raise ReviewAbandoned(f"review abandoned before {getattr(fn, '__name__', fn)}")
            self._running += 1
        try:
            return fn(*args, **kwargs)
        finally:
            with self._lock:
                self._running -= 1
                remove = self._closed and self._running == 0
            if remove:
                self._remove()

    async def run(self, fn: Callable[..., T], *args: Any, **kwargs: Any) -> T:
        """Run ``fn`` on a worker thread while holding the folder open."""
        return await asyncio.to_thread(self._call, fn, *args, **kwargs)


async def run_step(
    step: str,
    ids: tuple[str, str],
    fn: Callable[..., T],
    *args: Any,
    scratch: ReviewScratch | None = None,
    **kwargs: Any,
) -> T:
    """Log ``step``'s start, then run ``fn`` on a worker thread within the step bound.

    Raises :class:`ReviewStepTimeout` when the bound expires. The worker thread cannot be
    stopped; with a ``scratch`` it keeps the folder alive until it returns."""
    timeout = step_timeout_seconds()
    log_step_start(step, ids, timeout=timeout)
    call = scratch.run(fn, *args, **kwargs) if scratch else asyncio.to_thread(fn, *args, **kwargs)
    try:
        return await asyncio.wait_for(call, timeout=timeout)
    except (asyncio.TimeoutError, TimeoutError) as exc:
        raise ReviewStepTimeout(step, timeout) from exc


__all__ = [
    "SCRATCH_PREFIX",
    "STEP_TIMEOUT_DIVISOR",
    "ReviewAbandoned",
    "ReviewScratch",
    "ReviewStepTimeout",
    "log_step_start",
    "run_step",
    "step_timeout_seconds",
]
