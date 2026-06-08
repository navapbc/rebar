"""Event file sort key for chronological + type-ordered replay."""

from __future__ import annotations

import os

# LINK events must always replay before UNLINK at the same Unix-second timestamp,
# even when the UNLINK filename UUID sorts alphabetically before the LINK UUID (dso-jwan).
_EVENT_TYPE_ORDER: dict[str, int] = {"LINK": 0, "UNLINK": 1}


def event_sort_key(path: str) -> tuple[str, int, str]:
    """Sort key: (timestamp_segment, event_type_order, full_basename).

    - timestamp_segment: first '-'-delimited field preserves chronological order.
    - event_type_order: LINK=0, UNLINK=1 ensures LINK replays before UNLINK
      at the same Unix-second, even when UNLINK UUID sorts lower alphabetically.
    - full_basename: stable tiebreaker for remaining ambiguity within same type+timestamp.
    """
    name = os.path.basename(path)
    ts_segment = name.split("-")[0]
    stem = name[: -len(".json")] if name.endswith(".json") else name
    event_type = stem.rsplit("-", 1)[-1]
    return (ts_segment, _EVENT_TYPE_ORDER.get(event_type, 99), name)
