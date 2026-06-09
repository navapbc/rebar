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

import functools
from enum import Enum

# Ordered list defines < / > semantics for check_phase_gate.
# Index position IS the rank; do not reorder without updating tests.
_ORDERED = [
    "reconcile-check",
    "dry-run",
    "bootstrap-strict",
    "bootstrap-throttle",
    "live",
]


@functools.total_ordering
class Mode(str, Enum):
    """Reconciler operation mode.

    Members (rollout-safety set only):
        RECONCILE_CHECK   -- read-only discrepancy report; no writes
        DRY_RUN           -- read-only analysis; no Jira or ticket writes
        BOOTSTRAP_STRICT  -- conservative warm-up; writes only on high-confidence deltas
        BOOTSTRAP_THROTTLE -- permissive warm-up; writes on most deltas with rate-limiting
        LIVE              -- full production operation; no artificial throttling
    """

    RECONCILE_CHECK = "reconcile-check"
    DRY_RUN = "dry-run"
    BOOTSTRAP_STRICT = "bootstrap-strict"
    BOOTSTRAP_THROTTLE = "bootstrap-throttle"
    LIVE = "live"

    # ------------------------------------------------------------------
    # Construction
    # ------------------------------------------------------------------

    @classmethod
    def from_str(cls, value: str) -> "Mode":
        """Return the Mode whose string value matches *value*.

        Raises:
            ValueError: if *value* does not match any member.  The message
                lists all four allowed values verbatim so that callers can
                surface an actionable error to the user.
        """
        for m in cls:
            if m.value == value:
                return m
        allowed = ", ".join(repr(m.value) for m in cls)
        raise ValueError(f"unknown mode {value!r}; allowed: {allowed}")

    # ------------------------------------------------------------------
    # Ordering
    # ------------------------------------------------------------------

    def rank(self) -> int:
        """Return an integer rank for ordering comparisons.

        Ordering: dry-run (0) < bootstrap-strict (1) < bootstrap-throttle (2)
        < live (3).

        Backward-compat alias: the same ordering is now available natively via
        ``<``/``>`` operators (see ``__lt__`` and ``@functools.total_ordering``).
        New code should prefer the natural operators::

            if target_mode > gated_mode:
                raise PhaseGateError(...)
        """
        return _ORDERED.index(self.value)

    def __lt__(self, other: object) -> bool:
        """Order Modes by their position in ``_ORDERED``.

        Combined with ``@functools.total_ordering`` this yields the full set
        of comparison operators (``<``, ``<=``, ``>``, ``>=``) for free; ``==``
        comes from the Enum base class. Comparison against a non-Mode returns
        NotImplemented so Python can fall back to the reflected operator.
        """
        if not isinstance(other, Mode):
            return NotImplemented
        return _ORDERED.index(self.value) < _ORDERED.index(other.value)


# Per-mode mutation cap. None means uncapped (LIVE). 0 means "apply nothing"
# (DRY_RUN: all mutations are deferred, no leaf invoked). Finite positive caps
# (BOOTSTRAP_STRICT=10, BOOTSTRAP_THROTTLE=100) bound the blast radius of a
# single pass during the rollout phases. Used by applier.apply() to partition
# mutations into applied + deferred, in deterministic (direction, action, target)
# order.
MODE_CAPS: dict[Mode, int | None] = {
    Mode.RECONCILE_CHECK: 0,
    Mode.DRY_RUN: 0,
    Mode.BOOTSTRAP_STRICT: 10,
    Mode.BOOTSTRAP_THROTTLE: 100,
    Mode.LIVE: None,
}
