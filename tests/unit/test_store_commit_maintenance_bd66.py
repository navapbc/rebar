"""Keep lock-held commits separate from foreground maintenance (bd66-28a4-fd31-4c9f).

Git 2.47+ may run ``maintenance --auto`` inside ``git commit``. With ADR 0051's
``maintenance.autoDetach=false``, crossing the default loose-object threshold could put an
O(store) repack inside the ``_GIT_TIMEOUT`` subprocess, kill it mid-repack, and lose the write.
The fix retains the 30-second commit bound, suppresses that implicit maintenance, then runs an
explicit watchdog-budgeted step under the same lock. This test crosses the threshold, lowers
the commit timeout below repack duration, and requires the create to succeed.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

import rebar
from rebar._commands._seam import tracker_dir
from rebar._store import event_commit_git

pytestmark = pytest.mark.unit

# Comfortably past git's default gc.auto (~6700) so the create commit triggers a foreground
# repack, and large enough that the repack reliably exceeds the small bound below.
_LOOSE = 20000
# A per-commit bound a bare commit clears with wide margin but a 20k-object repack cannot.
_TINY_TIMEOUT = 1.0


def _git(d: str, *a: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    r = subprocess.run(["git", "-C", str(d), *a], capture_output=True, text=True, check=False)
    if check and r.returncode != 0:
        raise AssertionError(f"git {' '.join(a)} failed: {r.stderr}")
    return r


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    r = tmp_path / "repo"
    r.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=r, check=True)
    subprocess.run(["git", "config", "user.email", "t@e.com"], cwd=r, check=True)
    subprocess.run(["git", "config", "user.name", "T"], cwd=r, check=True)
    monkeypatch.setenv("REBAR_ROOT", str(r))
    rebar.init_repo(repo_root=str(r))
    return r


def _fill_loose(tracker: str, n: int) -> None:
    """Write *n* distinct loose objects into the tracker's (shared) object DB in ONE git
    process, so the next commit crosses git's gc.auto threshold and triggers a repack.

    ``git hash-object --stdin-paths`` reads a newline-separated list of file PATHS and writes
    one loose object per file, all within a single git invocation."""
    d = Path(tracker) / ".bd66_loose"
    d.mkdir(exist_ok=True)
    paths = []
    for i in range(n):
        p = d / f"b{i}"
        p.write_text(f"bd66-loose-{i}-{os.urandom(8).hex()}\n")
        paths.append(str(p))
    subprocess.run(
        ["git", "-C", tracker, "hash-object", "-w", "--stdin-paths"],
        input="\n".join(paths) + "\n",
        capture_output=True,
        text=True,
        check=True,
    )


def test_write_survives_foreground_maintenance_repack(
    repo: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    tracker = str(tracker_dir(str(repo)))
    # Precondition: rebar's gc-config makes auto-maintenance FOREGROUND (so it lands inside the
    # commit subprocess). If this ever changes, the mechanism under test no longer applies.
    assert _git(tracker, "config", "--get", "maintenance.autoDetach").stdout.strip() == "false"

    _fill_loose(tracker, _LOOSE)
    # Clean up the scratch worktree files so they don't become tracked noise; the loose objects
    # they created remain in the object DB.
    for p in (Path(tracker) / ".bd66_loose").glob("b*"):
        p.unlink()
    (Path(tracker) / ".bd66_loose").rmdir()
    assert int(_git(tracker, "count-objects").stdout.split()[0]) >= 6700

    # A per-commit latency bound the commit itself clears easily, but a foreground O(store)
    # repack cannot. With the defect present the create's commit repacks in the foreground and
    # is SIGKILLed at this bound -> StoreError("git timed out"). With the fix, the commit runs
    # with auto-maintenance suppressed and completes well under the bound; maintenance runs as a
    # separate, watchdog-budgeted step.
    monkeypatch.setattr(event_commit_git, "_GIT_TIMEOUT", _TINY_TIMEOUT)

    # The report's own reproduction: a fixture creating a ticket in the store. Must not raise.
    tid = rebar.create_ticket("task", "bd66 create after loose-fill", repo_root=str(repo))
    assert tid, "create_ticket returned no id"

    # The write is durable and the store is not wedged: a second create also succeeds.
    tid2 = rebar.create_ticket("task", "bd66 second create", repo_root=str(repo))
    assert tid2 and tid2 != tid
