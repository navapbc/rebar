"""Four-bucket comparison and the pure verdict function for the mechanism-delta ratchet.

:class:`Counters`, :func:`compare` and ``has_regression`` are ported essentially unchanged
from ``scripts/check_complexity_baseline.py`` because they are proven and their buckets are
mutually exclusive:

  * ``active``    — key in both the current tree and the baseline.
  * ``new``       — key in the current tree but NOT the baseline (a mechanism was ADDED).
  * ``increased`` — key in both with a value above its recorded one. Structurally
    unreachable while presence values are pinned at ``1``, and kept anyway so the ported
    contract stays whole and a future weighted value has a bucket waiting for it.
  * ``stale``     — key in the baseline with no current definition site (a mechanism was
    REMOVED). Removal is the direction this ratchet exists to reward, so it is an allowed
    improvement, never a regression.

:func:`evaluate` is PURE and INJECTABLE — it takes the current census, the baseline and the
marker map as plain dicts and returns ``(exit_code, lines)``. Nothing in it touches the
filesystem, so the whole verdict is testable without a tree.
"""

from __future__ import annotations


class Counters:
    """Mutually-exclusive per-mechanism classification buckets (lists of keys)."""

    def __init__(self) -> None:
        self.new: list[str] = []
        self.increased: list[str] = []
        self.active: list[str] = []
        self.stale: list[str] = []

    @property
    def has_regression(self) -> bool:
        return bool(self.new) or bool(self.increased)

    @property
    def summary(self) -> str:
        return (
            f"active={len(self.active)} new={len(self.new)} "
            f"increased={len(self.increased)} stale={len(self.stale)}"
        )


def compare(current: dict[str, int], baseline: dict[str, int]) -> Counters:
    """Classify each mechanism key into exactly one mutually-exclusive counter."""
    counters = Counters()
    for key, presence in current.items():
        if key not in baseline:
            counters.new.append(key)
        elif presence > baseline[key]:
            counters.increased.append(key)
        elif presence == baseline[key]:
            counters.active.append(key)
        else:
            counters.stale.append(key)
    for key in baseline:
        if key not in current:
            counters.stale.append(key)
    return counters


_ADMIT_HINT = (
    "        add '# mechanism-ok: <kind> <name> — <reason or ticket id>' at the "
    "definition site, or remove the mechanism"
)


def _blank_marker_errors(markers: dict[str, str]) -> list[str]:
    """One line per marker whose reason is blank — a rubber stamp is not a justification."""
    return [
        f"  marker    {key} — blank reason; "
        "'# mechanism-ok: <kind> <name> — <reason or ticket id>' requires one"
        for key in sorted(markers)
        if not markers[key].strip()
    ]


def _partition(keys: list[str], markers: dict[str, str]) -> tuple[list[str], list[str]]:
    """Split regression keys into ``(admitted, unadmitted)`` by exact-key marker."""
    admitted: list[str] = []
    unadmitted: list[str] = []
    for key in sorted(keys):
        reason = markers.get(key, "")
        (admitted if reason.strip() else unadmitted).append(key)
    return admitted, unadmitted


def evaluate(
    current: dict[str, int], baseline: dict[str, int], markers: dict[str, str]
) -> tuple[int, list[str]]:
    """Return ``(exit_code, report_lines)`` for a census/baseline/marker triple.

    Exit 0 requires that every ``new``/``increased`` mechanism carries a non-blank
    ``# mechanism-ok:`` marker for its EXACT key, and that no marker anywhere is blank.
    ``stale`` alone always passes — a removed mechanism is the outcome the ratchet wants.
    """
    counters = compare(current, baseline)
    lines = [counters.summary]
    errors = _blank_marker_errors(markers)

    new_ok, new_bad = _partition(counters.new, markers)
    inc_ok, inc_bad = _partition(counters.increased, markers)
    for key in new_ok + inc_ok:
        lines.append(f"  admitted  {key} — {markers[key].strip()}")
    for key in new_bad:
        lines.append(f"  new       {key} — a mechanism was added with no justification")
        lines.append(_ADMIT_HINT)
    for key in inc_bad:
        lines.append(f"  increased {key} — presence value above its recorded baseline")
    lines.extend(errors)
    return (1 if (new_bad or inc_bad or errors) else 0), lines


def drain_stale(current: dict[str, int], baseline: dict[str, int]) -> dict[str, int]:
    """Return the drained baseline for ``--update-stale`` (caller guards regressions).

    Drops entries whose definition site is gone and preserves every active entry. New
    mechanisms are NEVER folded in here: adding one is the regression this gate exists to
    catch, and the caller refuses to write while any is present.
    """
    return {key: 1 for key in baseline if key in current}
