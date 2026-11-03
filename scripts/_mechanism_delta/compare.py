"""Classify mechanism changes and evaluate the ratchet without filesystem access.

``compare`` assigns each key to one bucket. ``active`` is present at the recorded value.
``new`` lacks a baseline entry. ``increased`` exceeds its baseline value and is unreachable
for validated presence-only values. ``stale`` is below or absent from the current census and
passes by itself. ``evaluate`` accepts the census, baseline, and harvested markers as plain
inputs. ``drain_stale`` keeps active baseline keys without adding new keys.
"""

from __future__ import annotations

from .markers import sunset_ticket


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


_GRAMMAR = "'# mechanism-ok: <kind> <name> — <sunset-ticket-id> <reason>'"

_ADMIT_HINT = f"        add {_GRAMMAR} at the definition site, or remove the mechanism"

_ID_HINT = (
    f"{_GRAMMAR} requires an alias, two-quad, or four-quad id to open the reason "
    "(a bare 4-digit quad is deprecated, so it names nothing)"
)


def _marker_errors(markers: dict[str, str]) -> list[str]:
    """One line per marker that admits nothing — a blank reason, or one naming no sunset ticket.

    A marker whose reason is free prose admits its mechanism FOREVER: nothing can ever
    establish that the justification expired. Requiring a ticket id gives the admission an
    owner the close precheck can hold open.
    """
    errors: list[str] = []
    for key in sorted(markers):
        reason = markers[key].strip()
        if not reason:
            errors.append(f"  marker    {key} — blank reason; {_ID_HINT}")
        elif not sunset_ticket(reason):
            errors.append(f"  marker    {key} — names no sunset ticket; {_ID_HINT}")
    return errors


def _partition(keys: list[str], markers: dict[str, str]) -> tuple[list[str], list[str]]:
    """Split regression keys into ``(admitted, unadmitted)`` by exact-key marker.

    Only a marker naming a sunset ticket admits; a nonblank reason alone no longer does.
    """
    admitted: list[str] = []
    unadmitted: list[str] = []
    for key in sorted(keys):
        reason = markers.get(key, "")
        (admitted if sunset_ticket(reason) else unadmitted).append(key)
    return admitted, unadmitted


def evaluate(
    current: dict[str, int], baseline: dict[str, int], markers: dict[str, str]
) -> tuple[int, list[str]]:
    """Return a verdict that admits exact keys whose harvested marker names a sunset ticket.

    Every harvested marker needs a reason opening with a resolvable ticket id. A
    ``stale``-only census passes.
    """
    counters = compare(current, baseline)
    lines = [counters.summary]
    errors = _marker_errors(markers)

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
