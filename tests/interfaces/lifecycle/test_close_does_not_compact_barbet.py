"""Closing a ticket must NOT compact it (bug choosy-arthrodic-barbet).

THE DEFECT. The close's post-write tail called ``_compact_on_close``, which ran compaction
inline. ``compact_txn._compact_locked`` takes the ONE store write lock and holds it for the
whole fold — read, reduce, authorship ledger, snapshot write, retire renames, and the git
add/commit, whose nested ``_store_git_op_lock`` wait and index-lock retry budget stack inside
that hold with no aggregate ceiling. Measured on the rebar store, a single close held the lock
for 13m53s and three others the same hour held ~2.5 min each, starving every concurrent
writer. The 7084 stand-aside probe could not help: the closing process had released the lock
seconds earlier, so the store always read free to its own probe.

THE FIX (asserted here): compaction is off the close path entirely. It is OPTIONAL
housekeeping, never a correctness step — an unfolded event log is completely valid and the
reducer replays it — so removing the trigger changes performance and store size, never stored
state. ``rebar compact <id>`` still folds on demand.

Assertions are on OBSERVABLE artifacts only: the files in the ticket dir, the tracker's commit
subjects, and the reduced ticket state. Nothing here asserts a duration — the lock-scoping
claim is checked structurally, by sampling for the lock across the close and requiring that no
COMPACT lands, not by timing anything.
"""

from __future__ import annotations

import os
import subprocess
import threading
from pathlib import Path

import pytest

import rebar
from rebar._commands import compact as _compact
from rebar._store.ticket_layout import ticket_dir as layout_ticket_dir
from rebar.reducer import reduce_ticket

pytestmark = pytest.mark.interface


@pytest.fixture(autouse=True)
def _fold_everything(monkeypatch: pytest.MonkeyPatch) -> None:
    # Fold the whole log when compaction IS invoked (the standard lifecycle-test recipe).
    monkeypatch.setenv("REBAR_COMPACTION_HORIZON_NS", "0")


def _tdir(repo: Path, tid: str) -> Path:
    return Path(layout_ticket_dir(repo / ".tickets-tracker", tid))


def _commit_subjects(repo: Path) -> list[str]:
    proc = subprocess.run(
        ["git", "-C", str(repo / ".tickets-tracker"), "log", "--format=%s"],
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.splitlines()


def _seed_and_claim(repo: Path, title: str) -> str:
    """A ticket with several events, claimed so it can be closed."""
    tid = rebar.create_ticket("task", title, description="x" * 60, repo_root=str(repo))
    rebar.comment(tid, "c1", repo_root=str(repo))
    rebar.comment(tid, "c2", repo_root=str(repo))
    rebar.claim(tid, repo_root=str(repo))
    return tid


def _close(repo: Path, tid: str) -> None:
    rebar.transition(tid, "in_progress", "closed", repo_root=str(repo))


# ── the close writes no SNAPSHOT and retires no source ───────────────────────────────────────
def test_close_does_not_compact_the_ticket(rebar_repo: Path) -> None:
    """RED on the pre-fix code: ``_compact_on_close`` folded the log, so the ticket dir came
    out holding one ``-SNAPSHOT.json`` and a set of ``*.retired`` sources, and the tracker
    carried a ``ticket: COMPACT <id>`` commit."""
    repo = rebar_repo
    tid = _seed_and_claim(repo, "close must not compact")

    _close(repo, tid)

    tdir = _tdir(repo, tid)
    assert not list(tdir.glob("*-SNAPSHOT.json")), (
        "the close compacted the ticket — it wrote a SNAPSHOT; compaction must be out of band"
    )
    assert not list(tdir.glob("*.retired")), (
        "the close retired folded sources — compaction must not run on the close path"
    )
    assert not any(s.startswith(f"ticket: COMPACT {tid}") for s in _commit_subjects(repo)), (
        "the close produced a COMPACT commit"
    )


# ── the close still does its own job ─────────────────────────────────────────────────────────
def test_close_still_lands_its_status_write(rebar_repo: Path) -> None:
    """Removing compaction must not disturb the close itself: the STATUS event still commits
    and the ticket still reduces to ``closed``."""
    repo = rebar_repo
    tid = _seed_and_claim(repo, "close still closes")

    _close(repo, tid)

    assert any(s.startswith(f"ticket: STATUS {tid}") for s in _commit_subjects(repo)), (
        "the close did not commit its STATUS event"
    )
    state = reduce_ticket(str(_tdir(repo, tid)))
    assert state is not None and state.get("status") == "closed"
    # The events the close did NOT fold are still live and still replay.
    comments = [c.get("body") for c in (state.get("comments") or [])]
    assert "c1" in comments and "c2" in comments, (
        "unfolded events must stay live and replay — the reducer reads them directly"
    )


# ── compaction itself is intact, only its trigger is gone ────────────────────────────────────
def test_explicit_compact_still_folds_a_closed_ticket(rebar_repo: Path) -> None:
    """``rebar compact <id>`` on the just-closed ticket must still fold it — proof that this
    change removed the TRIGGER, not the capability."""
    repo = rebar_repo
    tid = _seed_and_claim(repo, "explicit compact still works")
    _close(repo, tid)

    rc = _compact.compact_cli([tid, "--threshold=0", "--skip-sync"], repo_root=str(repo))

    assert rc == 0, "explicit compaction must still succeed"
    tdir = _tdir(repo, tid)
    assert len(list(tdir.glob("*-SNAPSHOT.json"))) == 1, (
        "explicit compaction did not write the SNAPSHOT"
    )
    assert list(tdir.glob("*.retired")), "explicit compaction did not retire the folded sources"
    state = reduce_ticket(str(tdir))
    assert state is not None and state.get("status") == "closed", (
        "the folded ticket must reduce to the same state"
    )


# ── the store lock is never held across a fold during a close ────────────────────────────────
def test_close_never_holds_the_lock_across_a_fold(rebar_repo: Path) -> None:
    """Sample (lock held?, fold artifacts present?) every 20ms across a whole close and require
    that the two are NEVER true at the same instant.

    This is the direct, load-bearing oracle for the lock-scoping claim. Compaction writes its
    SNAPSHOT and renames the folded sources WHILE holding the store write lock, so on the
    pre-fix code a fold produced a long run of samples in which the lock dir existed and a
    SNAPSHOT already sat in the ticket dir. A close that does not compact can never produce
    such a sample, whatever the machine's speed.

    The oracle is structural, not temporal: it names a forbidden STATE, never a duration, so
    there is no timing budget to be flaky under load (and none for the wall-clock-assert gate
    to reject).
    """
    repo = rebar_repo
    tid = _seed_and_claim(repo, "lock is not held across a fold")
    lock_dir = repo / ".tickets-tracker" / ".ticket-write.lock.d"
    tdir = _tdir(repo, tid)

    locked_during_fold: list[str] = []
    stop = threading.Event()

    def sample() -> None:
        while not stop.is_set():
            if os.path.exists(lock_dir):
                folded = sorted(p.name for p in tdir.glob("*-SNAPSHOT.json")) + sorted(
                    p.name for p in tdir.glob("*.retired")
                )
                if folded:
                    locked_during_fold.append(",".join(folded))
            stop.wait(0.02)

    sampler = threading.Thread(target=sample, daemon=True)
    sampler.start()
    try:
        _close(repo, tid)
    finally:
        stop.set()
        sampler.join()

    assert not locked_during_fold, (
        "the store write lock was held while fold artifacts existed — compaction ran under the "
        f"lock during the close (saw: {locked_during_fold[0]})"
    )
    assert not list(tdir.glob("*-SNAPSHOT.json")) and not list(tdir.glob("*.retired")), (
        "the close produced fold artifacts, so compaction ran on the close path"
    )
    assert not lock_dir.exists(), "the close left the store write lock held"
