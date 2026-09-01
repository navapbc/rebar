"""Held-out: a folded prior SNAPSHOT is not a source EVENT (bug aea0).

When compaction folds a ticket a second time, the SUPERSEDED SNAPSHOT is one of the files
it folds. The normal path listed every folded file in the new snapshot's
``source_event_uuids``, including that snapshot — so once the superseded file was gone,
``fsck``'s ``snapshot_missing_sources`` check reported a perfectly healthy ticket as
damaged, and (post b636) as un-rebuildable.

Citing it is wrong on the merits: a snapshot's entire content IS its ``compiled_state``,
which the successor absorbed, so nothing is lost when it disappears. The REBUILD path
already skipped snapshots when building its source list; the two paths simply disagreed,
and the normal one was wrong.

Both directions are asserted, because the fix must not weaken the b636 guard that catches a
genuinely truncated log:
  * a folded SNAPSHOT is NOT cited;
  * a folded RAW event still IS cited, and a raw event that goes missing is still reported.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import rebar
from rebar._commands import compact as _compact
from rebar._store.ticket_layout import ticket_dir as layout_ticket_dir


def _events(repo: Path, tid: str) -> list[Path]:
    tdir = Path(layout_ticket_dir(repo / ".tickets-tracker", tid))
    return [p for p in tdir.glob("*.json") if not p.name.startswith(".")]


def _snapshot(repo: Path, tid: str) -> dict:
    snaps = [p for p in _events(repo, tid) if p.name.endswith("-SNAPSHOT.json")]
    assert len(snaps) == 1, f"expected exactly one active SNAPSHOT, got {snaps}"
    return json.loads(snaps[0].read_text(encoding="utf-8"))


def _compact_now(repo: Path, tid: str) -> None:
    rc = _compact.compact_cli([tid, "--threshold=0", "--skip-sync"], repo_root=str(repo))
    assert rc == 0


def test_folded_snapshot_is_not_cited_as_a_source(
    rebar_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    tid = rebar.create_ticket("task", "double-compact", repo_root=str(rebar_repo))
    rebar.comment(tid, "one", repo_root=str(rebar_repo))
    _compact_now(rebar_repo, tid)

    first = _snapshot(rebar_repo, tid)
    first_uuid = first["uuid"]
    assert first["data"]["source_event_uuids"], "the first fold should cite its raw sources"

    # A second fold: its inputs now INCLUDE the snapshot written above.
    rebar.comment(tid, "two", repo_root=str(rebar_repo))
    _compact_now(rebar_repo, tid)

    second = _snapshot(rebar_repo, tid)
    assert second["uuid"] != first_uuid
    cited = second["data"]["source_event_uuids"]
    assert first_uuid not in cited, (
        "the superseded SNAPSHOT was cited as a source event; once it is gone fsck reports "
        "this healthy ticket as damaged"
    )
    # ...and the raw event folded in the same pass IS still cited (no over-correction).
    assert cited, "the second fold dropped its raw sources too"
    capsys.readouterr()


def test_a_genuinely_missing_raw_source_is_still_reported(
    rebar_repo: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The b636 guard must not be weakened: a vanished RAW event is still a finding."""
    from rebar._commands import fsck as _fsck

    tid = rebar.create_ticket("task", "truncated", repo_root=str(rebar_repo))
    rebar.comment(tid, "one", repo_root=str(rebar_repo))
    _compact_now(rebar_repo, tid)
    capsys.readouterr()

    # Delete a retired RAW source the snapshot cites — a truncated log, not a fold.
    tdir = Path(layout_ticket_dir(rebar_repo / ".tickets-tracker", tid))
    retired = [p for p in tdir.glob("*.retired") if "-SNAPSHOT.json" not in p.name]
    assert retired, "expected retired raw sources after the fold"
    retired[0].unlink()

    _fsck.fsck_cli([], repo_root=str(rebar_repo))
    out = capsys.readouterr().out
    assert "SNAPSHOT_MISSING_SOURCES" in out, out
