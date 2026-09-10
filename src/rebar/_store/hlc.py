"""Hybrid Logical Clock for causally ordered event filenames.

Replay sorts the integer timestamp prefix. Wall-clock skew could otherwise let a
causally later edit sort first. :func:`next_tick` returns
``max(cache, ticket_event_prefixes, physical_now()) + 1``. The ticket witness
orders fetched peer events correctly, while the physical component preserves
approximate time order across unrelated clones.

Legacy nanosecond and HLC prefixes remain plain 19-digit integers until about year
2286. Reducers compare them numerically, and older string-ordering clients retain
the same width. Values exceed 2^53, so ``jq`` must not parse them.

The ignored ``.rebar/hlc.state`` is a per-clone high-water cache. Durable event
history remains authoritative, making missing or lost cache writes safe. A single
global cache and local lock provide stronger monotonicity than per-ticket caches.
The per-ticket witness supplies the causal floor after fetch.

Clock errors fall back to ``physical_now()`` so event writes continue.
``REBAR_HLC_NOW`` injects skewed test time. ``next_tick`` holds
``.rebar/hlc.lock`` only for its read-modify-write and never across the store lock.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from rebar._store.fsutil import sibling_exclusive_lock
from rebar._store.paths import StorePaths

logger = logging.getLogger(__name__)


def physical_now() -> int:
    """The physical clock source: ``time.time_ns()``, or the ``REBAR_HLC_NOW``
    override (the injection point the skewed-clock harness drives). A malformed
    override is ignored."""
    override = os.environ.get("REBAR_HLC_NOW")  # read-via: test-only-clock-injection
    if override is not None:
        try:
            return int(override.strip())
        except ValueError:
            pass
    return time.time_ns()


def _max_event_prefix(tracker: str | os.PathLike, ticket_id: str) -> int:
    """The largest integer filename-prefix among the TARGET ticket's committed
    event files (0 if none / unreadable). This is the cross-clone causal floor."""
    ticket_dir = Path(tracker) / ticket_id
    best = 0
    try:
        entries = os.listdir(ticket_dir)
    except OSError:
        return 0
    for name in entries:
        if name.startswith(".") or not name.endswith(".json"):
            continue
        seg = name.split("-", 1)[0]
        if seg.isdigit():
            v = int(seg)
            if v > best:
                best = v
    return best


@contextmanager
def _hlc_lock(rebar_dir: Path) -> Iterator[None]:
    """A dedicated, local exclusive lock on ``.rebar/hlc.lock`` — held only for the
    duration of one RMW, never across the store write lock (no ordering hazard)."""
    with sibling_exclusive_lock(rebar_dir / "hlc.state", lock_name="hlc.lock"):
        yield


def _read_state(rebar_dir: Path) -> int:
    try:
        return int((rebar_dir / "hlc.state").read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return 0


def _write_state(rebar_dir: Path, value: int) -> None:
    # Best-effort persist via a same-dir temp + atomic rename (a torn cache is
    # still correct — it is re-derived from the log on the next tick). No fsync:
    # the atomic replace is enough; the durable value rides in git.
    from rebar._store.fsutil import atomic_write

    atomic_write(rebar_dir / "hlc.state", str(value), encoding="utf-8")


def next_tick(tracker: str | os.PathLike, ticket_id: str) -> int:
    """Return the next event timestamp for ``ticket_id`` under ``tracker``.

    Performs the monotonic ``max(cache, witness, physical_now()) + 1`` RMW under
    the local ``.rebar/hlc.lock``. Any error in the enabled path falls back to
    ``physical_now()`` so a write never fails on the clock.
    """
    try:
        rebar_dir = Path(StorePaths(tracker).rebar_dir)
        witness = _max_event_prefix(tracker, ticket_id)
        with _hlc_lock(rebar_dir):
            tick = max(_read_state(rebar_dir), witness, physical_now()) + 1
            _write_state(rebar_dir, tick)
        return tick
    except Exception:
        logger.warning("HLC monotonic tick failed; falling back to physical clock", exc_info=True)
        return physical_now()
