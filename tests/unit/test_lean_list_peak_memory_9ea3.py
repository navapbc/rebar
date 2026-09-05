"""The lean discovery read must not materialize the fields it drops.

Regression oracle for witted-invisible-roan (9ea3-7d07-ea55-4496). The MCP server
OOM-killed the box because every discovery read compiles the WHOLE store into
memory at once and the lean projection -- which exists precisely because
``LEAN_OMITTED_FIELDS`` are ~88% of a discovery payload -- was applied only AFTER
that full list existed. Peak RSS therefore scaled as ``base + K * 295 MB`` with
``K`` = concurrent tool calls (measured linear at K=1,2,4,8), which on a 7.6 GiB
host OOMs before it can plateau.

The contract asserted here is about PEAK ALLOCATION, not wire shape: a lean list
must cost materially less to PRODUCE, not merely to serialize. It is a ratio
against the same store read both ways, so it is host- and store-size independent.
"""

from __future__ import annotations

import json
import tracemalloc
from pathlib import Path
from typing import Any

import pytest

from rebar._engine_support.reads import LEAN_OMITTED_FIELDS, list_states
from rebar._engine_support.ticket_query import TicketQuery

pytestmark = pytest.mark.unit

#: Tickets in the synthetic store, and the size of each omitted-field body.
_TICKETS = 40
_BODY_BYTES = 120_000


def _build_store(root: Path) -> None:
    """A tracker whose weight is concentrated ENTIRELY in ``LEAN_OMITTED_FIELDS``."""
    filler = "x" * _BODY_BYTES
    for i in range(_TICKETS):
        tid = f"{i:04d}-0000-0000-4000"
        tdir = root / tid
        tdir.mkdir()
        payload = {
            "timestamp": 100 + i,
            "uuid": f"{i:08d}-1111-4111-8111-111111111111",
            "event_type": "CREATE",
            "env_id": "00000000-0000-4000-8000-000000000001",
            "author": "Peak Tester",
            "data": {
                "ticket_type": "task",
                "title": f"ticket {i}",
                "parent_id": None,
                "description": filler,
            },
        }
        (tdir / f"{100 + i}-{payload['uuid']}-CREATE.json").write_text(
            json.dumps(payload), encoding="utf-8"
        )


def _peak_bytes(fn) -> tuple[int, Any]:
    """Peak tracemalloc bytes attributable to ``fn()``, with its result."""
    tracemalloc.start(4)
    try:
        tracemalloc.reset_peak()
        result = fn()
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    return peak, result


def test_lean_list_does_not_materialize_the_fields_it_drops(tmp_path: Path) -> None:
    tracker = tmp_path / ".tickets-tracker"
    tracker.mkdir()
    _build_store(tracker)

    lean_q = TicketQuery(include_body=False)
    full_q = TicketQuery(include_body=True)

    # Warm the per-ticket reducer caches BOTH ways so the comparison measures the
    # read, not first-compile cache population.
    list_states(str(tracker), full_q)
    list_states(str(tracker), lean_q)

    full_peak, full_rows = _peak_bytes(lambda: list_states(str(tracker), full_q))
    lean_peak, lean_rows = _peak_bytes(lambda: list_states(str(tracker), lean_q))

    assert len(full_rows) == _TICKETS
    assert len(lean_rows) == _TICKETS
    # Precondition: the lean row really does drop the heavy fields (wire shape).
    for row in lean_rows:
        for field in LEAN_OMITTED_FIELDS:
            assert field not in row
    # Precondition: the store's weight really is in the omitted fields, so a
    # failure below is about WHEN they are dropped, not about a thin fixture.
    assert full_peak > _TICKETS * _BODY_BYTES * 0.5, f"fixture too light: full peak {full_peak} B"

    # The contract: producing the lean list must cost materially less than the
    # full list. Today both compile every full state first, so the two peaks are
    # equal and this fails.
    assert lean_peak < full_peak * 0.5, (
        "the lean discovery read still materializes the fields it drops: "
        f"lean peak {lean_peak / 1e6:.1f} MB vs full peak {full_peak / 1e6:.1f} MB "
        "(expected the lean read to peak below half the full read)"
    )
