"""Keep lock-held commits separate from foreground maintenance (bd66-28a4-fd31-4c9f).

Git 2.47+ may run ``maintenance --auto`` inside ``git commit``. With ADR 0051's
``maintenance.autoDetach=false``, crossing the default loose-object threshold could put an
O(store) repack inside the ``_GIT_TIMEOUT`` subprocess, kill it mid-repack, and lose the write.
The fix retains the 30-second commit bound, suppresses that implicit maintenance, then runs an
explicit watchdog-budgeted step under the same lock. These tests cross the threshold and read
git's Trace2 process tree: no maintenance, gc or repack process may be a DESCENDANT of a
``git commit``. The invariant is structural. A wall-clock kill bound (the first version used
1 s) also fails on any commit starved by host load, with no maintenance involved (bug 99fb).
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest
from _git_upkeep import _trace_events

import rebar
from rebar._commands._seam import tracker_dir
from rebar._store import event_commit_git

pytestmark = pytest.mark.unit

# Comfortably past git's default gc.auto (~6700) so the create commit triggers a foreground
# repack when the suppression is absent.
_LOOSE = 20000


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


# Commands that make up the O(store) work the suppression keeps out of a bounded commit.
_MAINTENANCE_COMMANDS = frozenset(
    {"maintenance", "gc", "repack", "pack-objects", "multi-pack-index"}
)


def _git_subcommand(argv: list[str]) -> str | None:
    """The git subcommand in a traced argv, skipping the executable and ``-c``/``-C`` options."""
    i = 1
    while i < len(argv):
        if argv[i] in ("-c", "-C"):
            i += 2
        elif argv[i].startswith("-"):
            i += 1
        else:
            return argv[i]
    return None


def _maintenance_inside_commits(trace: Path) -> tuple[int, list[str]]:
    """``(commits traced, maintenance processes that are descendants of a commit)``.

    A process's Trace2 ``sid`` is its parent's ``sid`` plus ``/`` plus its own, so a commit's
    descendants are the processes whose ``sid`` starts with the commit's ``sid`` and a slash.
    The explicit post-commit maintenance step is a separate top-level process, so its repack
    children are not counted: only work done INSIDE a commit is.
    """
    starts = [
        (str(event.get("sid", "")), [str(a) for a in event.get("argv") or []])
        for event in _trace_events(trace)
        if event.get("event") == "start"
    ]
    commit_sids = {
        sid for sid, argv in starts if "/" not in sid and _git_subcommand(argv) == "commit"
    }
    offenders = [
        " ".join(argv[:4])
        for sid, argv in starts
        if any(sid.startswith(f"{c}/") for c in commit_sids)
        and _git_subcommand(argv) in _MAINTENANCE_COMMANDS
    ]
    return len(commit_sids), offenders


def _cross_the_maintenance_threshold(repo: Path) -> str:
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
    return tracker


def _create_two_tickets_traced(
    repo: Path, monkeypatch: pytest.MonkeyPatch, trace: Path
) -> tuple[int, list[str]]:
    monkeypatch.setenv("GIT_TRACE2_EVENT", str(trace))
    # The report's own reproduction: a fixture creating a ticket in the store. Must not raise.
    tid = rebar.create_ticket("task", "bd66 create after loose-fill", repo_root=str(repo))
    assert tid, "create_ticket returned no id"
    # The write is durable and the store is not wedged: a second create also succeeds.
    tid2 = rebar.create_ticket("task", "bd66 second create", repo_root=str(repo))
    assert tid2 and tid2 != tid
    return _maintenance_inside_commits(trace)


def test_write_survives_foreground_maintenance_repack(
    repo: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    _cross_the_maintenance_threshold(repo)

    commits, offenders = _create_two_tickets_traced(repo, monkeypatch, tmp_path / "trace.json")

    assert commits >= 2, f"the trace must contain the ticket-create commits (saw {commits})"
    assert not offenders, (
        "git ran maintenance INSIDE a bounded commit, so a repack can be killed mid-way and "
        f"lose the write: {offenders}"
    )


def test_the_trace_check_detects_maintenance_inside_a_commit(
    repo: Path, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The check must be able to fail: with the suppression removed, git runs
    ``maintenance run --auto`` (and the repack below it) as a child of the commit."""
    _cross_the_maintenance_threshold(repo)
    monkeypatch.setattr(event_commit_git, "_AUTOMAINT_OFF", ())

    commits, offenders = _create_two_tickets_traced(repo, monkeypatch, tmp_path / "trace.json")

    assert commits >= 1
    assert any("maintenance" in o for o in offenders), (
        f"removing the suppression produced no maintenance descendant of a commit: {offenders}"
    )
