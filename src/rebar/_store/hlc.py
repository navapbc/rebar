"""Hybrid Logical Clock for event ordering (P2.1, epic snappy-weed-ruin).

rebar orders replay by the ``${timestamp_ns}`` filename prefix. Under wall-clock
skew two agents editing the same field resolved by *last wall-clock writer* — a
silent clobber (invariant I8 admitted EDIT/COMMENT interleaving was only
"best-effort"). This module closes that gap with a single-integer Hybrid Logical
Clock, the design git-bug ships (a persisted Lamport clock, re-seeded by
witnessing ``max`` over the durable history; the local file is a disposable
cache, the authoritative value rides in git).

**The tick.** :func:`next_tick` returns
``max(cache, max(prefix of the TARGET ticket's events), physical_now()) + 1`` —
a single monotonic integer that tracks wall-clock ns (so order still follows real
time across unrelated clones) but never ties/inverts for causally-related events
from one actor (the ``+1`` floor). Witnessing the *ticket's own* ``max(prefix)``
is what gives cross-clone causal correctness: a clone that pulled another's event
sorts strictly after it, regardless of clock skew.

**Single integer, no width hazard.** There is no second fixed-width field, so
legacy 19-digit ns names and new HLC names are *both plain integers*; ordering
compares them as integers (``reducer._sort``). ``physical_now()`` is 19 digits
until ~year 2286 and the ``+1`` floor never advances ~10^9× past wall-clock, so
the width stays 19 — older clones that still string-compare order correctly too.
The prefix is >2^53, so **jq must never touch it** (it parses as float64 and
rounds); P1.0 already keeps jq out of the event path.

**Disposable cache, not source of truth.** ``.rebar/hlc.state`` (gitignored,
rebuildable) is one per-clone high-water-mark; the per-ticket witness supplies the
causal floor a global cache alone would miss right after a fetch. A missing,
stale, or race-lost cache is still correct because the tick is re-derived from the
durable log — the local-lock RMW is a fast path, not a correctness dependency.
Do **not** "fix" the global-cache / per-ticket-witness asymmetry by making the
cache per-ticket: that would weaken the monotonicity the single local lock gives.

**Staging.** The whole clock is behind ``REBAR_HLC`` (default-on; kill-switch
``REBAR_HLC=0`` reverts to raw ``physical_now()`` for one release). The RMW is
best-effort: any error falls back to ``physical_now()`` so a write never fails on
the clock. ``REBAR_HLC_NOW`` injects the physical source for the skewed-clock
tests. The dedicated ``.rebar/hlc.lock`` is acquired and released *inside*
``next_tick`` only — never held across the store write lock, so there is no
lock-ordering hazard.
"""

from __future__ import annotations

import os
import time
from contextlib import contextmanager
from pathlib import Path

_FALSY = {"0", "false", "no", "off", ""}


def _enabled() -> bool:
    """``REBAR_HLC`` gates the clock — default ON. ``REBAR_HLC=0`` (or false/no/off)
    is the one-release kill-switch back to raw ``physical_now()``."""
    val = os.environ.get("REBAR_HLC")
    if val is None:
        return True
    return val.strip().lower() not in _FALSY


def physical_now() -> int:
    """The physical clock source: ``time.time_ns()``, or the ``REBAR_HLC_NOW``
    override (the injection point the skewed-clock harness drives). A malformed
    override is ignored."""
    override = os.environ.get("REBAR_HLC_NOW")
    if override is not None:
        try:
            return int(override.strip())
        except ValueError:
            pass
    return time.time_ns()


def _rebar_dir(tracker: str | os.PathLike) -> Path:
    """The per-clone ``.rebar/`` state directory, beside the tracker
    (``<repo>/.tickets-tracker`` → ``<repo>/.rebar``)."""
    return Path(tracker).resolve().parent / ".rebar"


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
def _hlc_lock(rebar_dir: Path):
    """A dedicated, local exclusive lock on ``.rebar/hlc.lock`` — held only for the
    duration of one RMW, never across the store write lock (no ordering hazard)."""
    import fcntl

    rebar_dir.mkdir(parents=True, exist_ok=True)
    lock_path = rebar_dir / "hlc.lock"
    fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)


def _read_state(rebar_dir: Path) -> int:
    try:
        return int((rebar_dir / "hlc.state").read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return 0


def _write_state(rebar_dir: Path, value: int) -> None:
    # Best-effort persist via a same-dir temp + atomic rename (a torn cache is
    # still correct — it is re-derived from the log on the next tick).
    tmp = rebar_dir / f".hlc.state.tmp-{os.getpid()}"
    tmp.write_text(str(value), encoding="utf-8")
    os.replace(tmp, rebar_dir / "hlc.state")


def next_tick(tracker: str | os.PathLike, ticket_id: str) -> int:
    """Return the next event timestamp for ``ticket_id`` under ``tracker``.

    With the clock disabled (``REBAR_HLC=0``) this is exactly ``physical_now()`` —
    today's ``time.time_ns()`` behavior, for clean rollback. Enabled, it performs
    the monotonic ``max(cache, witness, physical_now()) + 1`` RMW under the local
    ``.rebar/hlc.lock``. Any error in the enabled path falls back to
    ``physical_now()`` so a write never fails on the clock.
    """
    if not _enabled():
        return physical_now()
    try:
        rebar_dir = _rebar_dir(tracker)
        witness = _max_event_prefix(tracker, ticket_id)
        with _hlc_lock(rebar_dir):
            tick = max(_read_state(rebar_dir), witness, physical_now()) + 1
            _write_state(rebar_dir, tick)
        return tick
    except Exception:
        return physical_now()
