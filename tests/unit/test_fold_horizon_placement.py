"""A positive-horizon fold places its SNAPSHOT at the fold boundary (stormproof-stormy-toad).

The horizon's contract is that an event written on another clone shortly before a fold, and
not yet fetched, sorts AFTER the SNAPSHOT and replays on top. That holds only if the SNAPSHOT
sits just after the newest event it folded. With no younger live event the fold used the
folding host's clock instead, so a TAG_DELTA written 24 s earlier on the MCP clone merged in
before the SNAPSHOT, uncited: an ``ORPHAN_EVENT`` whose tag change replay skipped (77b6-868f).

Clocks are pinned through ``hlc._clock``, so every timestamp below is deterministic.
"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

import rebar
from rebar._commands import compact as _compact
from rebar._commands.fsck_scan import _check_snapshot
from rebar._store import hlc
from rebar.reducer import reduce_ticket

pytestmark = pytest.mark.unit

_MIN_NS = 60_000_000_000
_HORIZON_NS = 30 * _MIN_NS
#: Far above the wall clock, so the store's HLC cache never outranks the pinned clock.
_T0 = 4_000_000_000_000_000_000


class _Clock:
    def __init__(self, now: int) -> None:
        self.now = now

    def __call__(self) -> int:
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> _Clock:
    pinned = _Clock(_T0)
    monkeypatch.setattr(hlc, "_clock", pinned)
    return pinned


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, clock: _Clock) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (
        ("git", "init", "-q"),
        ("git", "config", "user.email", "t@e.com"),
        ("git", "config", "user.name", "t"),
        ("git", "commit", "-q", "--allow-empty", "-m", "i"),
    ):
        subprocess.run(args, cwd=repo, check=True, capture_output=True)
    monkeypatch.setenv("REBAR_ROOT", str(repo))
    rebar.init_repo(repo_root=str(repo))
    return repo


def _tdir(repo: Path, tid: str) -> Path:
    return repo / ".tickets-tracker" / tid


def _live(repo: Path, tid: str) -> list[Path]:
    return sorted(p for p in _tdir(repo, tid).glob("*.json") if not p.name.startswith("."))


def _ts(path: Path) -> int:
    return int(path.name.split("-", 1)[0])


def _snapshot(repo: Path, tid: str) -> Path:
    snaps = [p for p in _live(repo, tid) if p.name.endswith("-SNAPSHOT.json")]
    assert len(snaps) == 1, snaps
    return snaps[0]


def _fold(repo: Path, tid: str, horizon: int) -> int:
    return _compact.compact_cli(
        [tid, "--threshold=0", f"--horizon={horizon}", "--skip-sync"], repo_root=str(repo)
    )


def _commit_all(repo: Path, msg: str) -> None:
    tracker = repo / ".tickets-tracker"
    for args in (("git", "add", "-A"), ("git", "commit", "-q", "-m", msg)):
        subprocess.run(args, cwd=tracker, check=True, capture_output=True)


def _orphans(repo: Path, tid: str, snap: Path) -> list[str]:
    findings = _check_snapshot(str(_tdir(repo, tid)), tid, snap.name)
    return [f for f in findings if f.startswith("ORPHAN_EVENT")]


def _seed(repo: Path, clock: _Clock) -> tuple[str, int]:
    """A ticket with 2 events at T0, and the newest event's timestamp."""
    tid = rebar.create_ticket("task", "placement", description="x" * 60, repo_root=str(repo))
    clock.now = _T0 + _MIN_NS
    rebar.comment(tid, "c0", repo_root=str(repo))
    resolved = rebar._ids.resolve_ticket_id(tid, str(repo / ".tickets-tracker"))
    assert resolved is not None
    return resolved, max(_ts(p) for p in _live(repo, resolved))


def _unfetched_tag(repo: Path, tid: str, clock: _Clock, at: int) -> tuple[Path, Path]:
    """Write a TAG_DELTA at ``at`` and hide its file, as on a clone that has not fetched it."""
    before = set(_live(repo, tid))
    clock.now = at
    rebar.tag(tid, "late", repo_root=str(repo))
    (tag_file,) = set(_live(repo, tid)) - before
    hidden = tag_file.with_name(f".hidden-{tag_file.name}")
    tag_file.rename(hidden)
    return tag_file, hidden


def test_fold_without_young_event_places_snapshot_at_fold_boundary(
    store: Path, clock: _Clock
) -> None:
    """The 77b6 sequence: a tag lands on another clone 24 s before the fold, which has no
    younger live event. The tag must replay after the merge, and fsck must stay clean."""
    tid, newest_folded = _seed(store, clock)
    fold_at = _T0 + 46 * _MIN_NS
    tag_file, hidden = _unfetched_tag(store, tid, clock, fold_at - 24 * 1_000_000_000)

    clock.now = fold_at
    assert _fold(store, tid, _HORIZON_NS) == 0
    snap = _snapshot(store, tid)
    assert _ts(snap) == newest_folded + 1, "SNAPSHOT must sit at the fold boundary"

    hidden.rename(tag_file)  # the union merge lands the other clone's commit
    _commit_all(store, "merge the other clone's TAG_DELTA")

    assert tag_file.name > snap.name, "the merged-in event must sort after the SNAPSHOT"
    state = reduce_ticket(str(_tdir(store, tid)))
    assert state is not None and "late" in state["tags"], state and state.get("tags")
    assert _orphans(store, tid, snap) == []


def test_fold_with_young_event_keeps_boundary_placement(store: Path, clock: _Clock) -> None:
    """A young live event already pinned the boundary; that placement is unchanged."""
    tid, newest_folded = _seed(store, clock)
    clock.now = _T0 + 40 * _MIN_NS
    rebar.comment(tid, "young", repo_root=str(store))

    clock.now = _T0 + 46 * _MIN_NS
    assert _fold(store, tid, _HORIZON_NS) == 0
    snap = _snapshot(store, tid)

    assert _ts(snap) == newest_folded + 1
    state = reduce_ticket(str(_tdir(store, tid)))
    assert state is not None
    assert [c["body"] for c in state["comments"]] == ["c0", "young"]


def test_zero_horizon_fold_keeps_clock_placement(store: Path, clock: _Clock) -> None:
    """``horizon=0`` folds everything at the fold clock (the archive path); unchanged."""
    tid, newest_folded = _seed(store, clock)
    fold_at = _T0 + 46 * _MIN_NS
    clock.now = fold_at

    assert _fold(store, tid, 0) == 0
    snap = _snapshot(store, tid)

    assert _ts(snap) > fold_at > newest_folded
    assert os.path.exists(snap)
    sources = json.loads(snap.read_text(encoding="utf-8"))["data"]["source_event_uuids"]
    assert len(sources) == 2


def _rebuild(repo: Path, tid: str) -> bool:
    from rebar._commands import compact_rebuild

    return compact_rebuild.rebuild_snapshot_from_full_log(
        str(repo / ".tickets-tracker"), tid, str(_tdir(repo, tid)), no_commit=True
    )


def _compacted_with_orphan(repo: Path, clock: _Clock) -> tuple[str, int, Path]:
    """A ticket folded at its clock, plus a merged-in COMMENT older than that SNAPSHOT.

    Returns the ticket, the newest raw event's timestamp, and the clock-placed SNAPSHOT."""
    tid, _ = _seed(repo, clock)
    before = set(_live(repo, tid))
    clock.now = _T0 + 2 * _MIN_NS
    rebar.comment(tid, "orphan", repo_root=str(repo))
    (orphan,) = set(_live(repo, tid)) - before
    hidden = orphan.with_name(f".hidden-{orphan.name}")
    orphan.rename(hidden)
    clock.now = _T0 + 10 * _MIN_NS
    assert _fold(repo, tid, 0) == 0
    prior = _snapshot(repo, tid)
    hidden.rename(orphan)
    assert _orphans(repo, tid, prior), "SETUP: the merged-in COMMENT must be an orphan"
    return tid, _ts(orphan), prior


def test_rebuild_places_snapshot_after_newest_raw_event(
    store: Path, clock: _Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Under a positive horizon the rebuild anchors on the newest raw event it absorbs, not
    on its clock and not on the clock-placed prior SNAPSHOT it replaces (valiant-supreme-erin)."""
    monkeypatch.setenv("REBAR_COMPACTION_HORIZON_NS", str(_HORIZON_NS))
    tid, newest_raw, prior = _compacted_with_orphan(store, clock)

    clock.now = _T0 + 46 * _MIN_NS
    assert _rebuild(store, tid)
    snap = _snapshot(store, tid)

    assert _ts(snap) == newest_raw + 1 < _ts(prior)
    state = reduce_ticket(str(_tdir(store, tid)))
    assert state is not None
    assert [c["body"] for c in state["comments"]] == ["c0", "orphan"]
    assert _orphans(store, tid, snap) == []


def test_zero_horizon_rebuild_keeps_clock_placement(store: Path, clock: _Clock) -> None:
    """``horizon=0`` rebuilds at the clock, as the zero-horizon fold does; unchanged."""
    tid, _newest_raw, _prior = _compacted_with_orphan(store, clock)

    repair_at = _T0 + 46 * _MIN_NS
    clock.now = repair_at
    assert _rebuild(store, tid)

    assert _ts(_snapshot(store, tid)) > repair_at


def test_rebuild_with_unreadable_horizon_aborts_untouched(
    store: Path, clock: _Clock, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No horizon means no placement rule, so the rebuild aborts before any mutation."""
    tid, _newest_raw, prior = _compacted_with_orphan(store, clock)
    before = sorted(p.name for p in _tdir(store, tid).iterdir())
    monkeypatch.setenv("REBAR_COMPACTION_HORIZON_NS", "not-a-number")

    assert not _rebuild(store, tid)

    assert sorted(p.name for p in _tdir(store, tid).iterdir()) == before
    assert _snapshot(store, tid) == prior
