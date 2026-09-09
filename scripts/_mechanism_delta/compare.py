"""Classify mechanism changes and evaluate the ratchet without filesystem access.

``compare`` assigns each key to one bucket. ``active`` is present at the recorded value.
``new`` lacks a baseline entry. ``increased`` exceeds its baseline value and is unreachable
for validated presence-only values. ``stale`` is below or absent from the current census and
passes by itself. ``evaluate`` accepts the census, baseline, and harvested markers as plain
inputs. ``drain_stale`` keeps active baseline keys without adding new keys.
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
    """Return a verdict that admits exact keys with nonblank harvested marker reasons.

    Every harvested marker needs a nonblank reason. A ``stale``-only census passes.
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
    """Keep active baseline keys without adding new current keys."""
    return {key: 1 for key in baseline if key in current}
