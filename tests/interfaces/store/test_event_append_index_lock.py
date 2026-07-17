"""A git ``index.lock`` contention on the tickets tracker is ridden out (retry) or
reclaimed-if-stale, not surfaced as a hard write failure (ticket middle-actinium-thrush /
snide-cut-mussel).

rebar's ``write_lock`` (dual-window fcntl + mkdir) serializes ALL rebar git ops on a tracker
across processes on a host — so a ``.git/index.lock`` present while a store write holds the
lock is NOT a live rebar peer's; it is an ORPHAN left by an abnormally-terminated git
(SIGKILL/OOM/FS-fault). git refuses the ``add``/``commit`` with ``Unable to create
'…/index.lock': File exists. Another git process seems to be running`` and the write fails
hard, losing the event — and, blocked behind the 300s stale threshold, wedges EVERY write for
5 minutes (the catastrophic Mode B cascade) unless the orphan is reclaimed at once.

The contract these tests pin:
  * a **provably-stale** lock (old mtime, crashed holder) is reclaimed and the write succeeds;
  * a **contended** lock that the holder releases mid-retry is ridden out and the write succeeds;
  * under the exclusive write lock a young lock is an ORPHAN and is force-reclaimed at once
    (``force=True``), so a locked write self-heals instead of wedging for 300s;
  * the UNLOCKED reclaim (``force=False``) still leaves a young lock intact — outside the write
    lock it may be a live peer's, so removing it risks index corruption;
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


def test_unlocked_reclaim_preserves_young_lock_but_force_reclaims_orphan(tmp_path: Path) -> None:
    """The UNLOCKED reclaim (``force=False``) must NOT remove a young lock — outside the write
    lock it may be a LIVE peer's, and removing it risks index corruption. Under the exclusive
    write lock, however (which serializes ALL rebar git on a tracker — verified: no natural
    index.lock contention), a young lock is provably an ORPHAN and IS force-reclaimed
    (``force=True``); the end-to-end locked-write self-heal is covered by
    ``test_orphan_index_lock_under_write_lock_self_heals`` in the transient-retry suite."""
    from rebar._store.gitutil import _reclaim_if_stale_index_lock

    tracker = _fresh_tracker(tmp_path, "young")
    lock = _index_lock_path(tracker)
    lock.write_text("")  # fresh mtime (age ~0), never released

    _reclaim_if_stale_index_lock(tracker, force=False)
    assert lock.exists(), "unlocked reclaim must leave a young lock intact (possible live peer)"

    _reclaim_if_stale_index_lock(tracker, force=True)
    assert not lock.exists(), "under the write lock a young lock is an orphan → force-reclaimed"


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
