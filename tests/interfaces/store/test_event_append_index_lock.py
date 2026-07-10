"""A git ``index.lock`` contention on the tickets tracker is ridden out (retry) or
reclaimed-if-stale, not surfaced as a hard write failure (ticket middle-actinium-thrush /
snide-cut-mussel).

rebar's own ``write_lock`` only serializes writes *within one clone*. The tickets tracker
is a shared git worktree, so concurrent rebar processes (or a crashed git that left a stale
lock) collide on git's own ``index.lock`` — which ``write_lock`` does not cover. git then
refuses the ``add``/``commit`` with ``Unable to create '…/index.lock': File exists. Another
git process seems to be running`` and the write fails hard, losing the event.

The contract these tests pin:
  * a **provably-stale** lock (old mtime, crashed holder) is reclaimed and the write succeeds;
  * a **contended** lock that the holder releases mid-retry is ridden out and the write succeeds;
  * a **live/young** lock that is never released is NOT reclaimed (safety: never nuke a lock
    a live peer may hold) — the write still fails rather than risk index corruption;
  * a non-lock failure is unaffected.

These tests use REAL git locks (a real file at the tracker's ``index.lock`` path), not mocks.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from pathlib import Path

import pytest

import rebar
from rebar import config
from rebar._store import event_append

# A short stale threshold so the retry path self-heals fast in tests. The implementation
# must expose the reclamation threshold as this module-level seconds value.
_STALE_S = getattr(event_append, "_INDEX_LOCK_STALE_S", 300)


def _fresh_tracker(tmp_path: Path, name: str) -> str:
    repo = tmp_path / name
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=repo, check=True)
    rebar.init_repo(repo_root=str(repo))
    return str(config.tracker_dir(str(repo)))


def _event(uuid: str) -> dict:
    return {
        "timestamp": 1700000000000000000,
        "uuid": uuid,
        "event_type": "COMMENT",
        "env_id": "e",
        "author": "a",
        "data": {"body": "x"},
    }


def _index_lock_path(tracker: str) -> Path:
    p = subprocess.run(
        ["git", "-C", tracker, "rev-parse", "--git-path", "index.lock"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    # --git-path may be relative to the tracker cwd.
    return Path(p) if os.path.isabs(p) else Path(tracker) / p


def _committed(tracker: str, ticket_id: str) -> bool:
    r = subprocess.run(["git", "-C", tracker, "log", "--oneline"], capture_output=True, text=True)
    return f"COMMENT {ticket_id}" in r.stdout


def test_stale_index_lock_is_reclaimed_and_write_succeeds(tmp_path: Path) -> None:
    tracker = _fresh_tracker(tmp_path, "stale")
    lock = _index_lock_path(tracker)
    lock.write_text("")  # a leftover lock from a crashed git process
    old = time.time() - (_STALE_S + 60)
    os.utime(lock, (old, old))

    rc = event_append.stage_and_commit(tracker, "tk-stale", _event("u-stale"))
    assert rc == 0
    assert _committed(tracker, "tk-stale")
    assert not lock.exists(), "the stale lock should have been reclaimed"


def test_contended_index_lock_cleared_during_backoff_succeeds(tmp_path: Path) -> None:
    tracker = _fresh_tracker(tmp_path, "contended")
    lock = _index_lock_path(tracker)
    lock.write_text("")  # a *fresh* lock: a live peer mid-write
    # Peer releases the lock shortly after — the write must ride it out on retry.
    threading.Timer(0.4, lambda: lock.exists() and lock.unlink()).start()

    rc = event_append.stage_and_commit(tracker, "tk-cont", _event("u-cont"))
    assert rc == 0
    assert _committed(tracker, "tk-cont")


def test_live_young_index_lock_is_not_reclaimed(tmp_path: Path) -> None:
    """A young lock that is never released must NOT be force-removed — reclaiming a lock a
    live peer holds risks index corruption. The write fails rather than nuke it."""
    tracker = _fresh_tracker(tmp_path, "young")
    lock = _index_lock_path(tracker)
    lock.write_text("")  # fresh mtime, never released

    with pytest.raises(event_append.StoreError):
        event_append.stage_and_commit(tracker, "tk-young", _event("u-young"))
    assert lock.exists(), "a live/young lock must be left intact (not reclaimed)"


def test_nonlock_failure_is_unaffected(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """A NON-lock git failure still surfaces immediately (no lock retry masks it)."""
    tracker = _fresh_tracker(tmp_path, "hard")
    real_run = event_append.subprocess.run
    calls = {"commit": 0}

    def fake_run(cmd, *a, **kw):  # noqa: ANN001
        if isinstance(cmd, list) and "commit" in cmd:
            calls["commit"] += 1
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="fatal: some other error")
        return real_run(cmd, *a, **kw)

    monkeypatch.setattr(event_append.subprocess, "run", fake_run)
    with pytest.raises(event_append.StoreError):
        event_append.stage_and_commit(tracker, "tk-hard", _event("u-hard"))
    assert calls["commit"] == 1, "a non-lock commit failure must not be retried"
