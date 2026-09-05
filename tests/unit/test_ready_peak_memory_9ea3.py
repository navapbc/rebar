"""``find_ready_tickets`` must not compile the whole store in full.

Second regression oracle for witted-invisible-roan (9ea3-7d07-ea55-4496). The
ready path is the one measured at ~295 MB per call on the real store, and it is
the tool whose 40-way concurrency produced the OOM. It legitimately RETURNS full
states (``rebar.ready`` -> ``ready_states`` -> ``public_state``, and MCP
``ready_tickets(full=True)`` depends on that), so the fix must reduce the peak
WITHOUT changing the returned shape.

The store below is deliberately shaped like the real one: most tickets are
closed, only a few are ready, and essentially all of the weight sits in
``description``. Compiling every ticket in full to return three of them is the
defect; returning those three in full is the contract.
"""

from __future__ import annotations

import json
import tracemalloc
from pathlib import Path

import pytest

from rebar.graph._ready import find_ready_tickets

pytestmark = pytest.mark.unit

_TOTAL = 40
_READY = 3
_BODY_BYTES = 120_000


def _build_store(root: Path) -> None:
    filler = "x" * _BODY_BYTES
    for i in range(_TOTAL):
        tid = f"{i:04d}-0000-0000-4000"
        tdir = root / tid
        tdir.mkdir()
        uid = f"{i:08d}-1111-4111-8111-111111111111"
        create = {
            "timestamp": 100 + i,
            "uuid": uid,
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
        (tdir / f"{100 + i}-{uid}-CREATE.json").write_text(json.dumps(create), encoding="utf-8")
        # Everything past the first _READY tickets is closed, so it is not ready.
        if i >= _READY:
            suid = f"{i:08d}-2222-4222-8222-222222222222"
            status = {
                "timestamp": 500 + i,
                "uuid": suid,
                "event_type": "STATUS",
                "env_id": "00000000-0000-4000-8000-000000000001",
                "author": "Peak Tester",
                "data": {"status": "closed"},
            }
            (tdir / f"{500 + i}-{suid}-STATUS.json").write_text(
                json.dumps(status), encoding="utf-8"
            )


def test_ready_does_not_compile_the_whole_store_in_full(tmp_path: Path) -> None:
    tracker = tmp_path / ".tickets-tracker"
    tracker.mkdir()
    _build_store(tracker)

    find_ready_tickets(str(tracker))  # warm the per-ticket reducer caches

    tracemalloc.start(4)
    try:
        tracemalloc.reset_peak()
        ready = find_ready_tickets(str(tracker))
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()

    # Contract, part 1 (unchanged): the ready subset comes back in FULL.
    assert len(ready) == _READY, f"expected {_READY} ready, got {len(ready)}"
    for state in ready:
        assert len(state.get("description", "")) == _BODY_BYTES, (
            "the ready path must still return full states"
        )

    # Contract, part 2 (the fix): peak must scale with the READY subset, not with
    # the whole store. Today every one of the 40 bodies is live at once.
    whole_store_bytes = _TOTAL * _BODY_BYTES
    budget = (_READY + 6) * _BODY_BYTES  # ready subset + generous slack
    assert peak < budget, (
        "find_ready_tickets still compiles the whole store in full: peak "
        f"{peak / 1e6:.1f} MB for {_READY} ready tickets out of {_TOTAL} "
        f"(whole-store bodies are {whole_store_bytes / 1e6:.1f} MB; "
        f"budget {budget / 1e6:.1f} MB)"
    )


def test_ready_omitted_fields_do_not_drift_from_the_lean_row() -> None:
    """The two spellings of the omitted-field set must stay identical.

    ``rebar.graph._ready`` cannot import ``rebar._engine_support.reads`` at module
    scope (``reads.list_states`` calls ``find_ready_tickets``, so the import is
    circular), which is why ``_READY_OMITTED_FIELDS`` is spelled separately. This
    guard is what makes that duplication safe: adding a seventh bulky field to
    ``LEAN_OMITTED_FIELDS`` without adding it here would silently put the whole
    store's copies of it back on the ready path's peak.
    """
    from rebar._engine_support.reads import LEAN_OMITTED_FIELDS
    from rebar.graph._ready import _READY_OMITTED_FIELDS

    assert set(_READY_OMITTED_FIELDS) == set(LEAN_OMITTED_FIELDS)
