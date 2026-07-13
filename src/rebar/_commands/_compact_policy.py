"""Pure compaction fold predicate (no IO).

Extracted from ``compact.py``'s ``_compact_locked`` so the fold decision — the
one place that decides whether an event is old enough to squash into the SNAPSHOT
— can be exercised (and mutation-tested) in isolation, apart from the
subprocess-heavy compaction orchestration around it. See docs/mutation-testing.md.
"""

from __future__ import annotations


def is_foldable(ts: int | None, now: int, horizon: int) -> bool:
    """True iff an event at HLC ``ts`` is old enough to fold at ``now`` under ``horizon``.

    ``horizon <= 0`` folds everything (the pre-RC2b behaviour / offline default);
    otherwise an event folds only once it is at least ``horizon`` HLC-ns old
    (``now - ts >= horizon``). A ``ts`` of ``None`` never folds under a positive
    horizon (its age is unknown).
    """
    return horizon <= 0 or (ts is not None and now - ts >= horizon)
