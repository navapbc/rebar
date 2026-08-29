"""bd66-28a4-fd31-4c9f — a lock-held store write must not be lost to ``_GIT_TIMEOUT`` while
git's post-commit FOREGROUND auto-maintenance runs an O(store) repack inside the commit.

Mechanism (see the ticket RCA): on git >= 2.47 ``git commit`` runs ``git maintenance run
--auto`` automatically, and ADR 0051 deliberately forces it FOREGROUND on the tickets worktree
(``maintenance.autoDetach=false``) so it serialises under the store write lock. That repack
therefore runs *inside* the same ``git commit`` subprocess that ``event_commit_git._run_git``
bounds with ``_GIT_TIMEOUT``. ``gc.auto`` is left at git's default (~6700 loose objects), so
once a store crosses that threshold the triggering commit pays the full O(store) repack cost;
on a large store it exceeds the per-commit bound and the write is SIGKILLed mid-repack — the
write is lost AND the kill lands mid-``git repack`` (the interrupted-maintenance corruption
ADR 0051 exists to prevent).

The fix keeps ``_GIT_TIMEOUT`` = 30 (the c2ba parity test is untouched) but suppresses git's
auto-maintenance ON the lock-held commit (so the bound covers only the commit) and runs
maintenance as an explicit, watchdog-budgeted step under the same write lock.

This test crosses the loose-object threshold, monkeypatches ``_GIT_TIMEOUT`` to a value the
foreground repack would exceed, and asserts the create still succeeds (the write is not lost).
It fails RED before the fix (the create raises ``git timed out``).
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
