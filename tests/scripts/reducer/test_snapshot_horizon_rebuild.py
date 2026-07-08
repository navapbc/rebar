"""RC2b (36d1) reducer-level coverage: ``include_retired`` full-log replay, the
rebuild path's cache bypass, and replay determinism.

The horizon fold guard and the fsck rebuild-on-stray remediation are exercised
end-to-end against a real store in
``tests/integration/test_concurrency_regression.py``; these tests pin the reducer
seams they rely on.
"""

from __future__ import annotations

import os
from pathlib import Path
from types import ModuleType

import pytest
from _events import _UUID, _UUID2, _write_event

pytestmark = [pytest.mark.unit, pytest.mark.scripts]

_CREATE_TS = 1_000_000_000_000
_STATUS_TS = 2_000_000_000_000


def _seed_create_plus_status(ticket_dir: Path) -> None:
    ticket_dir.mkdir()
    _write_event(
        ticket_dir,
        timestamp=_CREATE_TS,
        uuid=_UUID,
        event_type="CREATE",
        data={"ticket_type": "task", "title": "T", "status": "open"},
    )
    _write_event(
        ticket_dir,
        timestamp=_STATUS_TS,
        uuid=_UUID2,
        event_type="STATUS",
        data={"status": "in_progress"},
    )


def test_include_retired_replays_folded_sources(tmp_path: Path, reducer: ModuleType) -> None:
    """A ``*.retired`` source is invisible to a normal reduce but replayed by
    ``include_retired=True`` — the mechanism that lets a rebuild reconstruct state a
    stale snapshot's positional skip dropped."""
    ticket_dir = tmp_path / "tkt-retired"
    _seed_create_plus_status(ticket_dir)

    assert reducer.reduce_ticket(ticket_dir)["status"] == "in_progress"

    # Retire the STATUS event (as compaction would).
    status_file = next(ticket_dir.glob(f"*-{_UUID2}-STATUS.json"))
    status_file.rename(status_file.with_suffix(".json.retired"))

    # Normal reduce ignores the retired source → falls back to the CREATE status.
    assert reducer.reduce_ticket(ticket_dir)["status"] == "open"
    # Rebuild mode folds it back in.
    assert reducer.reduce_ticket(ticket_dir, include_retired=True)["status"] == "in_progress"


def test_rebuild_include_retired_bypasses_stale_cache(tmp_path: Path, reducer: ModuleType) -> None:
    """The rebuild path must not return a stale active-only cache entry."""
    ticket_dir = tmp_path / "tkt-cache"
    _seed_create_plus_status(ticket_dir)

    # Prime the reducer cache with the active-only ('open' after retiring) state.
    status_file = next(ticket_dir.glob(f"*-{_UUID2}-STATUS.json"))
    status_file.rename(status_file.with_suffix(".json.retired"))
    assert reducer.reduce_ticket(ticket_dir)["status"] == "open"
    assert (ticket_dir / ".cache.json").exists(), "active-only reduce should have cached"

    # include_retired must recompute from the full log, not serve the cached 'open'.
    assert reducer.reduce_ticket(ticket_dir, include_retired=True)["status"] == "in_progress"


def test_reduce_determinism_under_shuffled_listdir(
    tmp_path: Path, reducer: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``reduce_ticket`` is order-independent: the same file set yields the same
    compiled_state regardless of ``os.listdir`` ordering (replay sorts by the HLC
    prefix). Proof for the determinism AC."""
    ticket_dir = tmp_path / "tkt-determinism"
    _seed_create_plus_status(ticket_dir)

    from rebar.reducer import _cache as _cache_mod

    real_listdir = os.listdir

    def _forward(path):  # noqa: ANN001
        return list(real_listdir(path))

    def _reversed(path):  # noqa: ANN001
        return list(reversed(real_listdir(path)))

    monkeypatch.setattr(_cache_mod.os, "listdir", _forward)
    first = reducer.reduce_ticket(ticket_dir)
    (ticket_dir / ".cache.json").unlink(missing_ok=True)  # force a fresh replay
    monkeypatch.setattr(_cache_mod.os, "listdir", _reversed)
    second = reducer.reduce_ticket(ticket_dir)

    drop = ("updated_at",)
    assert {k: v for k, v in first.items() if k not in drop} == {
        k: v for k, v in second.items() if k not in drop
    }
