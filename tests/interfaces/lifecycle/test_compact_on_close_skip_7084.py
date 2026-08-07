"""compact-on-close stands aside when the store write lock is busy (bug 7084 / R3).

Compaction is the store's longest lock holder — a measured 48.1s ``compact-on-close``
consumed six concurrent writers' entire 60s acquire budget and all six writes were lost —
and its work is entirely OPTIONAL: an unfolded event log is completely valid. So when the
lock is already busy the fold is skipped and left for a later, uncontended run.

The load-bearing property is that skipping is a NO-OP FOR CORRECTNESS, not a silent data
change: the close itself still succeeds, the raw events stay live and readable, and the
next compaction folds exactly what the skipped one would have. All three are asserted
here, against a real store with a real second holder of the lock.
"""

from __future__ import annotations

import logging
from pathlib import Path

import rebar
from rebar._commands import compact as _compact
from rebar._commands import transition_close
from rebar._store import lock as _lock


def _tracker(repo: Path) -> str:
    return str(repo / ".tickets-tracker")


def _seed(repo: Path, title: str) -> str:
    tid = rebar.create_ticket(
        "task",
        title,
        description="Body.\n\n## Acceptance Criteria\n- [ ] a",
        repo_root=str(repo),
    )
    rebar.transition(tid, "open", "in_progress", repo_root=str(repo))
    rebar.comment(tid, "note one", repo_root=str(repo))
    rebar.comment(tid, "note two", repo_root=str(repo))
    return tid


def _events(repo: Path, tid: str) -> list[str]:
    tdir = repo / ".tickets-tracker" / tid
    return sorted(p.name for p in tdir.glob("*.json") if not p.name.startswith("."))


def _has_snapshot(repo: Path, tid: str) -> bool:
    return any(name.endswith("-SNAPSHOT.json") for name in _events(repo, tid))


def test_compact_on_close_skips_while_the_lock_is_busy(rebar_repo: Path, caplog) -> None:
    tid = _seed(rebar_repo, "skip-while-busy")
    before = _events(rebar_repo, tid)
    assert not _has_snapshot(rebar_repo, tid)

    handle = _lock.acquire(_tracker(rebar_repo), timeout=5, attempts=1)
    try:
        with caplog.at_level(logging.WARNING):
            transition_close._compact_on_close(str(rebar_repo), tid)
    finally:
        handle.release()

    # Nothing folded, nothing lost: the event log is byte-for-byte what it was.
    assert _events(rebar_repo, tid) == before
    assert not _has_snapshot(rebar_repo, tid)
    # And the skip is FINDABLE — a compaction that quietly never runs must not be
    # indistinguishable from one that ran.
    assert any("compact-on-close skipped" in r.getMessage() for r in caplog.records)


def test_a_skipped_fold_is_picked_up_by_the_next_compaction(rebar_repo: Path) -> None:
    """The reason no forced-after-N-skips floor is required under intermittent load: the
    deferred work is not lost, it is simply done later — and it folds exactly what the
    skipped run would have."""
    tid = _seed(rebar_repo, "deferred-fold")
    before = _events(rebar_repo, tid)

    handle = _lock.acquire(_tracker(rebar_repo), timeout=5, attempts=1)
    try:
        transition_close._compact_on_close(str(rebar_repo), tid)
    finally:
        handle.release()
    assert _events(rebar_repo, tid) == before

    # The very next (uncontended) compaction folds them.
    transition_close._compact_on_close(str(rebar_repo), tid)
    assert _has_snapshot(rebar_repo, tid)

    # The folded state is the SAME state the raw log reduced to.
    state = rebar.show_ticket(tid, repo_root=str(rebar_repo))
    assert state["status"] == "in_progress"
    assert [c["body"] for c in state["comments"]] == ["note one", "note two"]


def test_the_close_itself_still_succeeds_when_compaction_is_skipped(
    rebar_repo: Path, monkeypatch
) -> None:
    """Skipping must not weaken the close: compact-on-close was already best-effort, and
    a skip is just an earlier, cheaper way of not folding."""
    tid = _seed(rebar_repo, "close-with-skip")

    calls: list[str] = []
    real_cli = _compact.compact_cli

    def spy(argv, **kw):
        calls.append(argv[0])
        return real_cli(argv, **kw)

    monkeypatch.setattr(_compact, "compact_cli", spy)
    handle = _lock.acquire(_tracker(rebar_repo), timeout=5, attempts=1)
    try:
        transition_close._compact_on_close(str(rebar_repo), tid)
    finally:
        handle.release()

    assert calls == [], "compaction must not even be attempted while the lock is busy"

    # The close path itself is unaffected.
    rebar.transition(tid, "in_progress", "closed", repo_root=str(rebar_repo))
    assert rebar.show_ticket(tid, repo_root=str(rebar_repo))["status"] == "closed"


def test_compaction_still_runs_when_the_lock_is_free(rebar_repo: Path) -> None:
    """The skip is conditional, not a disablement."""
    tid = _seed(rebar_repo, "still-compacts")

    transition_close._compact_on_close(str(rebar_repo), tid)

    assert _has_snapshot(rebar_repo, tid)
