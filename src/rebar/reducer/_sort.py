"""Event file sort key for chronological + type-ordered replay.

The timestamp filename-prefix is compared as an **integer** (P2.1 / HLC): with a
single-integer prefix, legacy 19-digit ns names and new HLC names are both plain
integers, and integer comparison orders them into one global order regardless of
digit width — string comparison only agrees while every name has the *same* width.
``prefix_ts`` is the single shared comparator the other filename-order sites
(``graph/_links``, ``_commands/unlink``, ``_commands/txn``) import.
"""

from __future__ import annotations

import os

# LINK events must always replay before UNLINK at the same timestamp,
# even when the UNLINK filename UUID sorts alphabetically before the LINK UUID.
_EVENT_TYPE_ORDER: dict[str, int] = {"LINK": 0, "UNLINK": 1}


def prefix_ts(name: str) -> int:
    """Integer value of an event filename's ``${timestamp}`` prefix; ``-1`` for a
    malformed/prefixless name (sorts before any real event, deterministically — the
    full-name tiebreak keeps order stable). Real events are always ``{int}-...``."""
    seg = os.path.basename(name).split("-", 1)[0]
    return int(seg) if seg.isdigit() else -1


def event_sort_key(path: str) -> tuple[int, int, str]:
    """Sort key: (timestamp_prefix_int, event_type_order, full_basename).

    - timestamp_prefix_int: integer prefix preserves chronological/causal order
      across mixed digit widths (HLC); see module docstring.
    - event_type_order: LINK=0, UNLINK=1 ensures LINK replays before UNLINK
      at the same timestamp, even when UNLINK UUID sorts lower alphabetically.
    - full_basename: stable tiebreaker for remaining ambiguity within same type+timestamp.
    """
    name = os.path.basename(path)
    stem = name[: -len(".json")] if name.endswith(".json") else name
    event_type = stem.rsplit("-", 1)[-1]
    return (prefix_ts(name), _EVENT_TYPE_ORDER.get(event_type, 99), name)
