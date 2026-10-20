"""Reconciler operation mode enum.

Mode controls what the reconciler does during each reconciliation cycle.
These modes are the ROLLOUT-SAFETY modes and are ORTHOGONAL to the
drift-injection modes used by inject-and-heal.sh (orphan, mislabel,
missing-prop), which are shell-script parameters, not passed to reconcile.py.

Ordering (ascending by operational impact):
    dry-run (0) < bootstrap-strict (1) < bootstrap-throttle (2) < live (3)

dry-run is special: it performs read-only diff analysis with no writes.
The bootstrap modes are progressive warm-up phases before full live operation.
"""

from __future__ import annotations

from enum import Enum

# Ordered list defines < / > semantics for check_phase_gate.
# Index position IS the rank; do not reorder without updating tests.
_ORDERED = [
    "dry-run",
    "bootstrap-strict",
    "bootstrap-throttle",
    "live",
]


class Mode(str, Enum):
    """Reconciler operation mode.

    Members (rollout-safety set only):
        DRY_RUN           -- read-only discrepancy report; no Jira or ticket writes
        BOOTSTRAP_STRICT  -- conservative warm-up; writes only on high-confidence deltas
        BOOTSTRAP_THROTTLE -- permissive warm-up; writes on most deltas with rate-limiting
        LIVE              -- full production operation; no artificial throttling
    """

    DRY_RUN = "dry-run"
    BOOTSTRAP_STRICT = "bootstrap-strict"
    BOOTSTRAP_THROTTLE = "bootstrap-throttle"
    LIVE = "live"

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    @classmethod
    def from_str(cls, value: str) -> Mode:
        """Return the Mode whose string value matches *value*.

        Raises:
            ValueError: if *value* does not match any member.  The message
                lists all four allowed values verbatim so that callers can
                surface an actionable error to the user.
        """
        if value == "reconcile-check":
            # AC3 historical-data carve-out: _ref_lock pause blobs can still
            # carry this persisted sentinel while the live mode taxonomy uses
            # dry-run as the cap-0 read-only floor.
            return cls.DRY_RUN
        for m in cls:
            if m.value == value:
                return m
        allowed = ", ".join(repr(m.value) for m in cls)
        raise ValueError(f"unknown mode {value!r}; allowed: {allowed}")

    # ------------------------------------------------------------------
    # Ordering
    # ------------------------------------------------------------------

    @staticmethod
    def _compatible_rank(other: object) -> int | None:
        """Return another mode member's rank across supported module-load aliases."""
        if not isinstance(other, Enum):
            return None
        value = other.value
        if not isinstance(value, str):
            return None
        try:
            return _ORDERED.index(value)
        except ValueError:
            return None

    def __lt__(self, other: object) -> bool:
        """Order Modes by their position in ``_ORDERED``."""
        other_rank = self._compatible_rank(other)
        if other_rank is None:
            return NotImplemented
        return _ORDERED.index(self.value) < other_rank

    def __le__(self, other: object) -> bool:
        """Order Modes by their position in ``_ORDERED``."""
        other_rank = self._compatible_rank(other)
        if other_rank is None:
            return NotImplemented
        return _ORDERED.index(self.value) <= other_rank

    def __gt__(self, other: object) -> bool:
        """Order Modes by their position in ``_ORDERED``."""
        other_rank = self._compatible_rank(other)
        if other_rank is None:
            return NotImplemented
        return _ORDERED.index(self.value) > other_rank

    def __ge__(self, other: object) -> bool:
        """Order Modes by their position in ``_ORDERED``."""
        other_rank = self._compatible_rank(other)
        if other_rank is None:
            return NotImplemented
        return _ORDERED.index(self.value) >= other_rank


# Per-mode mutation cap. None means uncapped (LIVE). 0 means "apply nothing"
# (DRY_RUN: all mutations are deferred, no leaf invoked). Finite positive caps
# (BOOTSTRAP_STRICT=10, BOOTSTRAP_THROTTLE=100) bound the blast radius of a
# single pass during the rollout phases. Used by applier.apply() to partition
# mutations into applied + deferred, in deterministic (direction, action, target)
# order.
MODE_CAPS: dict[Mode, int | None] = {
    Mode.DRY_RUN: 0,
    Mode.BOOTSTRAP_STRICT: 10,
    Mode.BOOTSTRAP_THROTTLE: 100,
    Mode.LIVE: None,
}
