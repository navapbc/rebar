"""The rebuild counter still counts after the split (task b2bb).

``rebuild_snapshot_from_full_log`` increments a MODULE-GLOBAL ``_REBUILD_COUNT`` via
``global``. That makes the counter and its incrementer inseparable: if ``get_rebuild_count``
were left behind in ``compact.py`` while the function moved to ``compact_rebuild.py``, it
would read a DIFFERENT module's global and report zero forever — a silent regression that
re-exporting the name does not catch, because the import still resolves.

Nothing else in the suite exercises the counter (no caller outside ``compact.py`` reads it),
so without this test the split could land green with the observability quietly dead. Asserts
the increment through the RE-EXPORTED path, which is the one callers use.
"""

from __future__ import annotations

from pathlib import Path

import rebar
from rebar._commands import compact as _compact
from rebar._store.ticket_layout import ticket_dir as layout_ticket_dir


def test_rebuild_increments_the_counter_through_the_reexport(rebar_repo: Path) -> None:
    tid = rebar.create_ticket("task", "counter", repo_root=str(rebar_repo))
    rebar.comment(tid, "one", repo_root=str(rebar_repo))
    tracker = rebar_repo / ".tickets-tracker"
    ticket_dir = Path(layout_ticket_dir(tracker, tid))

    before = _compact.get_rebuild_count()
    rebuilt = _compact.rebuild_snapshot_from_full_log(
        str(tracker), tid, str(ticket_dir), no_commit=True
    )
    assert rebuilt is True, "the rebuild did not run, so the counter proves nothing"

    after = _compact.get_rebuild_count()
    assert after == before + 1, (
        "get_rebuild_count did not observe the rebuild — the counter and its incrementer "
        "are bound to different module globals"
    )

    # The counter is the same object either way round: reading it off the module that OWNS
    # it must agree with reading it off the facade that re-exports it.
    from rebar._commands import compact_rebuild as _compact_rebuild

    assert _compact_rebuild.get_rebuild_count() == after
