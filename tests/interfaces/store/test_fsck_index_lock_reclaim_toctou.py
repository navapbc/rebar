"""fsck Check 3 stale ``index.lock`` reclaim shares the write-path TOCTOU hardening.

Bug 4c6c: fsck's Check 3 reclaimed a stale ``.git/index.lock`` with a hand-rolled
check->use sequence (``os.path.getmtime`` judges it stale, then a raw
``os.remove`` unlinks by pathname) with NO identity re-validation between the two.
A concurrent writer that removes the stale lock and drops a FRESH LIVE lock at the
same path in that window gets its live lock clobbered — the exact TOCTOU already
fixed for the write path (df83 / sundried-bonny-sloth) via
``gitutil._reclaim_if_stale_index_lock`` (device+inode+age re-validation before
unlink). The fix routes Check 3's mutate branch through that hardened helper.

The peer file-swap is injected deterministically (no sleeps/processes/load): for
the fixed code through the helper's ``_reclaim_probe`` seam (fires after the stale
decision, before the guarded removal); for the pre-fix code through a wrap on
``os.path.getmtime`` (the pre-fix staleness check) that fires the same swap in the
same window. Exactly one hook fires per code version; the swap is idempotent.
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from rebar._commands import fsck
from rebar._store import gitutil


def _resolved_index_lock(tracker: str) -> Path:
    """The git ``index.lock`` path Check 3 targets, via the production resolver."""
    git_dir = fsck._resolve_tracker_git_dir(tracker)
    assert git_dir, "tracker git dir should resolve"
    return Path(git_dir) / "index.lock"


def _seed_stale_lock(lock_file: Path) -> None:
    lock_file.write_text("stale-owner")
    old = time.time() - (gitutil._INDEX_LOCK_STALE_S + 60)
    os.utime(lock_file, (old, old))


def test_fsck_check3_does_not_clobber_fresh_lock_replaced_midflight(rebar_repo: Path, monkeypatch):
    """A peer removes the stale ``index.lock`` and drops a FRESH LIVE lock at the same
    path in the window between fsck Check 3's staleness decision and its unlink. The
    peer's fresh lock MUST survive (Check 3 re-validates device+inode+age before
    removing, aborting on mismatch). Against the pre-fix raw ``os.remove`` this FAILS
    (the fresh lock is clobbered)."""
    tracker = str(rebar_repo / ".tickets-tracker")
    lock_file = _resolved_index_lock(tracker)
    _seed_stale_lock(lock_file)

    fresh_marker = "peer-fresh-owner"
    state = {"swapped": False}

    def _peer_replaces_lock() -> None:
        # The peer removes our stale lock and drops a fresh live one (fresh mtime;
        # the OS may reuse the freed inode number). Idempotent: fires exactly once.
        if state["swapped"]:
            return
        state["swapped"] = True
        lock_file.unlink()
        lock_file.write_text(fresh_marker)

    # Hook for the FIXED code path (Check 3 -> hardened helper): the helper's probe
    # fires after the stale decision and before the guarded re-validation + unlink.
    monkeypatch.setattr(gitutil, "_reclaim_probe", _peer_replaces_lock)

    # Hook for the PRE-FIX code path (Check 3's raw check->use): the peer swap fires
    # right after the staleness check (getmtime) and before the raw os.remove. The
    # fixed Check 3 never calls getmtime, so this hook is inert there.
    _real_getmtime = os.path.getmtime

    def _getmtime_then_swap(path):  # noqa: ANN001
        result = _real_getmtime(path)
        try:
            same = os.path.samefile(path, lock_file) if os.path.exists(path) else False
        except OSError:
            same = os.fspath(path) == str(lock_file)
        if same or os.fspath(path) == str(lock_file):
            _peer_replaces_lock()
        return result

    monkeypatch.setattr(os.path, "getmtime", _getmtime_then_swap)

    fsck._scan(tracker, False, str(rebar_repo))

    assert lock_file.exists(), "the peer's fresh live index.lock was wrongly reclaimed"
    assert lock_file.read_text() == fresh_marker


def test_fsck_check3_still_reclaims_a_stationary_stale_lock(rebar_repo: Path):
    """The hardening must not regress the base case: a genuinely stale lock that is NOT
    replaced mid-flight is still reclaimed (unlinked) and reported FIXED."""
    tracker = str(rebar_repo / ".tickets-tracker")
    lock_file = _resolved_index_lock(tracker)
    _seed_stale_lock(lock_file)

    lines, _ = fsck._scan(tracker, False, str(rebar_repo))

    assert not lock_file.exists(), "a stationary stale lock should be reclaimed"
    assert any("FIXED: removed stale .git/index.lock" in ln for ln in lines)
