"""Bound every lock-held Git call in ``event_append`` at the local index bound.

Bug c2ba (melting-resting-serpent) replaced unbounded ``subprocess.run`` calls held under the
MKDIR write lock with ``event_append._run_git``, which applies ``_GIT_TIMEOUT`` and folds
:class:`subprocess.TimeoutExpired` into return code 124 so callers fail cleanly and release the
lock. c2ba set that bound to match the network push bound (30 s).

Bug evolutive-descendent-manta: every call on this path is local index work (``add``,
``commit``, ``rm``, ``checkout``, ``reset`` and recovery reads), whose cost grows with the number
of index entries, not with the network. A 213,713-entry tracker with a stale stat cache took
~49 s per index refresh, so its commits timed out at 30 s and the writes were lost. Following
the sync ruling (painted-epidermal-shelduck), the event write path now uses gitutil's
``_LOCAL_GIT_TIMEOUT``; network operations keep the push bound.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
from _subprocess_env import subprocess_env

from rebar._store import event_append, event_commit_git, gitutil, sync
from rebar._store import push as _push
from rebar._store.event_append import StoreError

pytestmark = pytest.mark.unit

# The incident's measured index refresh, and the scale that shrinks it and every bound
# together so the reproduction runs in seconds while keeping their ratios.
_MEASURED_REFRESH_S = 49
_SCALE = 1 / 30


def _git(d, *a, check=True):
    r = subprocess.run(
        ["git", "-C", str(d), *a],
        capture_output=True,
        text=True,
        check=False,
        env=subprocess_env(),
    )
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


@pytest.fixture
def slow_index_commit(tracker: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> float:
    """Stall every ``git -C <tracker> … commit`` by the scaled index refresh, then run it.

    A ``git`` wrapper on ``PATH`` hands the commit to ``perl``, which sleeps and then ``exec``s
    the real git in the same process. A watchdog kill therefore lands on the one process doing
    the work, sleeping or committing, and nothing is orphaned. Other git calls pass straight
    through. Returns the stall in seconds."""
    real_git = shutil.which("git")
    perl = shutil.which("perl")
    assert real_git
    if perl is None:
        pytest.skip("perl is needed to stall git in-process")
    stall = _MEASURED_REFRESH_S * _SCALE
    bindir = tmp_path / "bin"
    bindir.mkdir()
    wrapper = bindir / "git"
    wrapper.write_text(
        "#!/bin/sh\n"
        f'if [ "$1" = "-C" ] && [ "$2" = "{tracker}" ]; then\n'
        '  for a in "$@"; do\n'
        '    if [ "$a" = "commit" ]; then\n'
        f"      exec \"{perl}\" -e 'select(undef, undef, undef, shift); exec {{ $ARGV[0] }} @ARGV'"
        f' "{stall}" "{real_git}" "$@"\n'
        "    fi\n"
        "  done\n"
        "fi\n"
        f'exec "{real_git}" "$@"\n'
    )
    wrapper.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bindir}{os.pathsep}{os.environ['PATH']}")
    return stall


def _commit_count(tracker: str) -> int:
    return int(_git(tracker, "rev-list", "--count", "HEAD").stdout.strip())


def test_event_write_bound_is_the_local_index_bound():
    """The event write path reuses gitutil's local bound, the one ``run_git_write`` and sync's
    index-wide operations already use, and it is longer than the network bound."""
    assert event_append._GIT_TIMEOUT == gitutil._LOCAL_GIT_TIMEOUT
    assert event_commit_git._GIT_TIMEOUT == gitutil._LOCAL_GIT_TIMEOUT
    assert event_append._GIT_TIMEOUT > _push._GIT_TIMEOUT


def test_network_git_keeps_the_push_bound():
    """Push and sync's network calls keep the 30 s bound this change leaves alone."""
    assert _push._GIT_TIMEOUT == sync._GIT_TIMEOUT == 30


def test_slow_index_commit_loses_the_write_at_the_push_bound(
    tracker, slow_index_commit, monkeypatch
):
    """Reproduction: at the old push-sized bound, a commit slowed by the measured index refresh
    is killed, so the write fails and nothing is committed. This also proves the stall is real,
    so the passing case below is not vacuous."""
    monkeypatch.setattr(event_commit_git, "_GIT_TIMEOUT", _push._GIT_TIMEOUT * _SCALE)
    before = _commit_count(tracker)
    with pytest.raises(StoreError, match="timed out"):
        event_append.stage_and_commit(tracker, "tk", _event("u-old", 1700000000000000000))
    assert _commit_count(tracker) == before


def test_slow_index_commit_survives_at_the_event_write_bound(
    tracker, slow_index_commit, monkeypatch
):
    """Regression: at the event write path's own bound, scaled the same way, the slow commit
    finishes and the write is durable."""
    monkeypatch.setattr(event_commit_git, "_GIT_TIMEOUT", event_commit_git._GIT_TIMEOUT * _SCALE)
    before = _commit_count(tracker)
    rc = event_append.stage_and_commit(tracker, "tk", _event("u-new", 1700000000000000001))
    assert rc == 0
    assert _commit_count(tracker) == before + 1


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
    monkeypatch.setattr(gitutil, "_run_in_group", _spy)
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
    monkeypatch.setattr(gitutil, "_run_in_group", _boom)
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
    monkeypatch.setattr(gitutil, "_run_in_group", _spy)
    rc = event_append.stage_and_commit(tracker, "tk", _event("u-A", 1700000000000000000))
    assert rc == 0
    assert timeouts, "no git subprocess call was launched by the append path"
    assert all(t is not None and t > 0 for t in timeouts), (
        f"a lock-held git call ran without a timeout: {timeouts}"
    )
