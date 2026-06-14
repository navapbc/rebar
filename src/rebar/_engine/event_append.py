"""Shared event-file append — the ONE place a ticket event file is written.

rebar historically had three independent event-file writers: bash
``write_commit_event`` (ticket-lib.sh), ``ticket_txn.py`` (the optimistic-
concurrency transaction), and the reconciler's ``applier._write_event_file``. The
reconciler's writer took **no** ``.ticket-write.lock`` and so was not serialized
against a concurrent local agent's staging (ticket pokey-matte-flute).

This module centralises the two invariants every event write must honour:

* **I2 — filename contract:** ``{timestamp_ns}-{uuid}-{TYPE}.json`` (lexical ==
  chronological; globally unique so concurrent writers never collide and git
  merges them as a union). See ``event_filename``.
* **I5 — write lock:** mutations to a tracker take an exclusive ``flock`` on
  ``<tracker>/.ticket-write.lock`` so a local agent and the reconciler cannot
  interleave a half-written event. See ``write_lock``.

``append_event`` writes ONE event atomically (temp file + ``os.replace``) and
assumes the caller holds the write lock — so a multi-event transaction
(ticket_txn) can hold the lock once across several appends + its commit, while a
single-event writer (the reconciler) wraps one append in ``write_lock``.

Stdlib-only and dependency-free so every engine writer (and future strangler-fig
Tier B ports) can import it.
"""

from __future__ import annotations

import fcntl
import json
import os
import sys
import time
import uuid as _uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from collections.abc import Iterator

WRITE_LOCK_NAME = ".ticket-write.lock"


def event_filename(timestamp: int, uuid_str: str, event_type: str) -> str:
    """The I2 filename for an event: ``{timestamp_ns}-{uuid}-{TYPE}.json``."""
    return f"{timestamp}-{uuid_str}-{event_type}.json"


@contextmanager
def write_lock(tracker_dir: str | os.PathLike, timeout: float = 30.0) -> Iterator[None]:
    """Hold the exclusive ``.ticket-write.lock`` for the tracker (I5).

    Since Tier D this delegates to the ONE unified lock
    (:func:`rebar._store.lock.write_lock`), which takes BOTH ``fcntl.flock`` and the
    mkdir leg so the reconciler mutually excludes bash leaf-writes on every platform
    class (the stiff-mop-lane fix). Falls back to the historical fcntl-only acquire
    if the ``rebar`` package is not importable (defensive — keeps this module's
    stdlib-only contract intact for any bare-engine caller)."""
    try:
        _src = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
        if _src not in sys.path:
            sys.path.insert(0, _src)
        from rebar._store import lock as _store_lock
    except Exception:
        _store_lock = None

    if _store_lock is not None:
        try:
            with _store_lock.write_lock(tracker_dir, timeout=int(timeout), attempts=1, dual_window=True):
                yield
        except _store_lock.LockTimeout as exc:
            # Preserve this function's historical TimeoutError contract.
            raise TimeoutError(str(exc)) from None
        return

    # Fallback: historical fcntl-only acquire.
    lock_path = os.path.join(str(tracker_dir), WRITE_LOCK_NAME)
    fd = os.open(lock_path, os.O_CREAT | os.O_RDWR)
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise TimeoutError(
                        f"could not acquire {WRITE_LOCK_NAME} within {timeout}s"
                    )
                time.sleep(0.05)
        yield
    finally:
        os.close(fd)  # closing the fd releases the flock


def append_event(
    ticket_dir: str | os.PathLike,
    event_type: str,
    data: dict[str, Any],
    *,
    timestamp: int,
    uuid_str: str,
    env_id: str = "",
    author: str = "",
) -> Path:
    """Atomically append ONE event file to ``ticket_dir`` under the I2 filename.

    Writes to a temp file then ``os.replace`` (atomic on POSIX) so a reader never
    observes a half-written event. The caller MUST already hold the write lock
    (use :func:`write_lock`) — this function does not acquire it, so a multi-event
    transaction can append several events under one lock. Returns the final path.
    """
    tdir = Path(ticket_dir)
    tdir.mkdir(parents=True, exist_ok=True)
    event = {
        "timestamp": timestamp,
        "uuid": uuid_str,
        "event_type": event_type,
        "env_id": env_id,
        "author": author,
        "data": data,
    }
    final = tdir / event_filename(timestamp, uuid_str, event_type)
    tmp = tdir / f".tmp-{uuid_str}-{event_type}"
    tmp.write_text(json.dumps(event, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, final)
    return final


def new_event_id() -> tuple[int, str]:
    """A fresh ``(timestamp_ns, uuid4_str)`` pair for an event."""
    return time.time_ns(), str(_uuid.uuid4())
