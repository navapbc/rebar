"""A timed-out bounded git is reaped as a process group, SIGTERM first (bug 4eed-2e15).

``subprocess.run(timeout=)`` SIGKILLed only git itself: its ``.keep`` files stayed behind and
its children ran on outside rebar's write lock. ``run_git``'s ``subprocess.run`` semantics are
pinned by ``test_run_git_wrapper.py``.
"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from collections.abc import Iterator
from pathlib import Path

import pytest

from rebar._store import gitutil

pytestmark = [
    pytest.mark.unit,
    pytest.mark.skipif(sys.platform == "win32", reason="process-group reaping is POSIX"),
]

# Stand-in for a fetch that outlives its bound: create a pack ``.keep`` the way fetch-pack
# does, delete it only from a catchable-signal handler, and keep a child busy.
_FAKE_GIT = """#!/bin/sh
dir="$2"
touch "$dir/pack-1.keep"
trap 'rm -f "$dir/pack-1.keep"; exit 143' TERM
sleep 60 &
echo $! > "$dir/child.pid"
wait
"""


@pytest.fixture
def stalled_git(tmp_path: Path) -> Iterator[Path]:
    bindir = tmp_path / "bin"
    bindir.mkdir()
    fake = bindir / "git"
    fake.write_text(_FAKE_GIT)
    fake.chmod(0o755)
    work = tmp_path / "work"
    work.mkdir()
    real_path = os.environ["PATH"]
    os.environ["PATH"] = f"{bindir}{os.pathsep}{real_path}"
    try:
        yield work
    finally:
        os.environ["PATH"] = real_path
        if (work / "child.pid").exists() and _alive(pid := int((work / "child.pid").read_text())):
            os.kill(pid, 9)  # reap the stand-in's child even when the fix is absent


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def _assert_group_reaped(work: Path) -> None:
    assert not (work / "pack-1.keep").exists(), "SIGTERM must reach git so it drops its .keep"
    child = int((work / "child.pid").read_text())
    deadline = time.monotonic() + 10
    while _alive(child) and time.monotonic() < deadline:
        time.sleep(0.05)  # an orphaned zombie lingers until init reaps it
    assert not _alive(child), "no git descendant may outlive the bounded call"


def test_bounded_fetch_timeout_reaps_the_whole_group(stalled_git: Path) -> None:
    result = gitutil.run_git_bounded(stalled_git, "fetch", "origin", timeout=1.0)
    assert result.returncode == 124
    _assert_group_reaped(stalled_git)


def test_run_git_timeout_reaps_the_group_and_still_raises(stalled_git: Path) -> None:
    with pytest.raises(subprocess.TimeoutExpired):
        gitutil.run_git(stalled_git, "fetch", "origin", check=False, timeout=1.0)
    _assert_group_reaped(stalled_git)
