"""Bound every lock-held Git call in ``event_append`` (c2ba AC3).

Add, commit, and recovery commands previously used unbounded ``subprocess.run`` while holding
the MKDIR write lock. ``event_append._run_git`` now mirrors ``push._git``: apply
``_GIT_TIMEOUT`` and convert :class:`subprocess.TimeoutExpired` to return code 124. Existing
callers then fail cleanly and release the lock instead of hanging or leaving it orphaned.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from rebar._store import event_append
from rebar._store import push as _push

pytestmark = pytest.mark.unit


def _git(d, *a, check=True):
    r = subprocess.run(["git", "-C", str(d), *a], capture_output=True, text=True, check=False)
    if check and r.returncode != 0:
        raise AssertionError(f"git {' '.join(a)} failed: {r.stderr}")
    return r


def _event(uuid: str, ts: int) -> dict:
    return {
        "timestamp": ts,
        "uuid": uuid,
        "event_type": "COMMENT",
        "env_id": "e",
        "author": "a",
        "data": {"body": uuid},
    }


@pytest.fixture
def tracker(tmp_path: Path) -> str:
    td = tmp_path / "trk"
    td.mkdir()
    _git(td, "init", "-q", "-b", "tickets")
    _git(td, "config", "user.email", "t@e.com")
    _git(td, "config", "user.name", "T")
    (td / "seed").write_text("seed\n")
    _git(td, "add", "-A")
    _git(td, "commit", "-q", "-m", "seed")
    return str(td)


def test_git_timeout_constant_matches_push():
    """The bound is single-valued across the store's git plumbing: event_append reuses the
    same constant push.py already applies, so 'matching push.py' is enforced, not aspirational."""
    assert event_append._GIT_TIMEOUT == _push._GIT_TIMEOUT


def test_run_git_passes_the_timeout(monkeypatch):
    """Every git child launched by event_append carries ``timeout=_GIT_TIMEOUT``."""
    real_run = subprocess.run
    seen: dict = {}

    def _spy(argv, *a, **k):
        # Record the target call's timeout, but DELEGATE to real git so any other
        # subprocess.run in the window (this patches the shared subprocess.run) is unaffected.
        if isinstance(argv, list) and argv[:2] == ["git", "--version"]:
            seen["timeout"] = k.get("timeout")
        return real_run(argv, *a, **k)

    monkeypatch.setattr(event_append.subprocess, "run", _spy)
    event_append._run_git(["git", "--version"])
    assert seen["timeout"] == event_append._GIT_TIMEOUT
    assert seen["timeout"] is not None and seen["timeout"] > 0


def test_run_git_folds_timeout_into_a_synthetic_failure(monkeypatch):
    """A hung git must not raise out of the write-lock region as a TimeoutExpired; it is folded
    into a returncode-124 failure (mirroring push.py._git) so the caller's existing
    returncode-inspecting error path fails the write and releases the lock."""
    real_run = subprocess.run

    def _boom(argv, *a, **k):
        # Only the target call times out; delegate everything else to real git so this patch of
        # the shared subprocess.run cannot disturb unrelated calls in the window.
        if isinstance(argv, list) and argv[-1:] == ["--sentinel-c2ba"]:
            raise subprocess.TimeoutExpired(argv, k.get("timeout") or 1)
        return real_run(argv, *a, **k)

    monkeypatch.setattr(event_append.subprocess, "run", _boom)
    result = event_append._run_git(["git", "status", "--sentinel-c2ba"])
    assert result.returncode == 124
    assert "timed out" in (result.stderr or "")


def test_stage_and_commit_git_calls_are_all_timeout_bounded(tracker, monkeypatch):
    """Wiring: a real append holds the write lock across git add + git commit; assert EVERY git
    child it launches carries a positive timeout (no unbounded lock-held call remains)."""
    real_run = subprocess.run
    timeouts: list = []

    def _spy(cmd, *a, **k):
        if isinstance(cmd, list) and cmd[:1] == ["git"]:
            timeouts.append(k.get("timeout"))
        return real_run(cmd, *a, **k)

    monkeypatch.setattr(event_append.subprocess, "run", _spy)
    rc = event_append.stage_and_commit(tracker, "tk", _event("u-A", 1700000000000000000))
    assert rc == 0
    assert timeouts, "no git subprocess call was launched by the append path"
    assert all(t is not None and t > 0 for t in timeouts), (
        f"a lock-held git call ran without a timeout: {timeouts}"
    )
