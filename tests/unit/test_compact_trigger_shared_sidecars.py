"""The compaction trigger's lock and stamp belong to the STORE, not to the directory beside it.

Bug exemplary-silty-tuatara. In the deployed MCP container the tracker
(``/var/gerrit/site/mcp-tickets``) is a volume shared by the blue and green containers, but its
sibling ``.rebar/`` sits on each container's own writable layer. Keyed on ``.rebar``, every
deploy started with no sweep stamp (so the first close fired a full sweep regardless of the
interval) and the two containers held two different "one compactor at a time" locks over one
store. The simulation below gives each process its own ``.rebar`` for the same store, which is
exactly what two containers see.

The lock keeps its original name ``compact-worker.lock``; only its directory moved.

No autouse fixtures. Every child is started through ``_subprocess_env.subprocess_env``
and bounded by a ``communicate`` timeout, then killed and reaped on every path.
"""

from __future__ import annotations

import os
import select
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest
from _subprocess_env import subprocess_env

from rebar._commands import compact_trigger
from rebar._store.paths import StorePaths

pytestmark = pytest.mark.unit

_CHILD_BOUND_S = 60

# One process standing in for one container: its OWN `.rebar` (the writable layer), the shared
# tracker (the volume). It runs one action, prints the result, and for "hold" keeps the lock
# until its stdin closes, so the parent controls the overlap window.
_CHILD = textwrap.dedent(
    """
    import sys
    from rebar._store import paths
    from rebar._commands import compact_trigger as ct

    tracker, own_rebar, action = sys.argv[1], sys.argv[2], sys.argv[3]
    paths._rebar_dir = lambda _tracker: own_rebar  # this "container's" writable-layer .rebar
    if action == "hold":
        fd = ct._acquire_trigger_lock(tracker)
        print("acquired" if fd is not None else "refused", flush=True)
        sys.stdin.read()  # hold until the parent closes our stdin
        if fd is not None:
            ct.release_trigger_lock(tracker, fd)
    elif action == "try":
        fd = ct._acquire_trigger_lock(tracker)
        print("acquired" if fd is not None else "refused", flush=True)
        if fd is not None:
            ct.release_trigger_lock(tracker, fd)
    elif action == "stamp":
        ct.record_sweep(tracker)
        print("stamped", flush=True)
    elif action == "stale":
        print("stale" if ct._sweep_is_stale(tracker, 3600) else "fresh", flush=True)
    """
)


def _git_store(tmp_path: Path) -> str:
    """A git-backed tracker, as every rebar store is. Returns its (canonical) path."""
    tracker = Path(os.path.realpath(tmp_path)) / "site" / "mcp-tickets"
    tracker.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=tracker, check=True, capture_output=True)
    return str(tracker)


def _container_rebar(tmp_path: Path, name: str) -> str:
    own = Path(os.path.realpath(tmp_path)) / name / ".rebar"
    own.mkdir(parents=True)
    return str(own)


def _spawn(tracker: str, own_rebar: str, action: str) -> subprocess.Popen[str]:
    root = str(Path(__file__).resolve().parents[2])
    env = subprocess_env(PYTHONPATH=os.pathsep.join([root, os.path.join(root, "src")]))
    return subprocess.Popen(
        [sys.executable, "-c", _CHILD, tracker, own_rebar, action],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )


def _reap(proc: subprocess.Popen[str]) -> None:
    if proc.poll() is None:
        proc.kill()
    try:
        proc.communicate(timeout=_CHILD_BOUND_S)
    except (subprocess.TimeoutExpired, ValueError, OSError):
        pass


def _readline(proc: subprocess.Popen[str]) -> str:
    """One line of the child's stdout, waiting at most the child bound."""
    assert proc.stdout is not None
    ready, _, _ = select.select([proc.stdout], [], [], _CHILD_BOUND_S)
    assert ready, "child printed nothing within its bound"
    return proc.stdout.readline().strip()


def _run(tracker: str, own_rebar: str, action: str) -> str:
    proc = _spawn(tracker, own_rebar, action)
    try:
        out, err = proc.communicate("", timeout=_CHILD_BOUND_S)
    finally:
        _reap(proc)
    assert proc.returncode == 0, err
    return out.strip()


def test_two_processes_with_different_rebar_dirs_exclude_each_other(tmp_path: Path) -> None:
    """The blue-green overlap: one store, two `.rebar` dirs, ONE compactor at a time."""
    tracker = _git_store(tmp_path)
    blue = _container_rebar(tmp_path, "blue")
    green = _container_rebar(tmp_path, "green")

    holder = _spawn(tracker, blue, "hold")
    try:
        assert _readline(holder) == "acquired", "precondition: blue must acquire"
        assert _run(tracker, green, "try") == "refused", (
            "a second process over the SAME store took the compaction lock concurrently"
        )
        assert holder.stdin is not None
        holder.stdin.close()  # blue releases
        assert holder.wait(timeout=_CHILD_BOUND_S) == 0
        # Negative control: the refusal above came from blue's live lock, not a stuck file.
        assert _run(tracker, green, "try") == "acquired"
    finally:
        _reap(holder)


def test_a_sweep_stamped_by_one_process_reads_fresh_in_another(tmp_path: Path) -> None:
    """The deploy case: a new container's first close must not re-fire a sweep the store had."""
    tracker = _git_store(tmp_path)
    old = _container_rebar(tmp_path, "old")
    new = _container_rebar(tmp_path, "new")

    assert _run(tracker, new, "stale") == "stale", "precondition: a never-swept store is stale"
    assert _run(tracker, old, "stamp") == "stamped"
    assert _run(tracker, new, "stale") == "fresh", (
        "a process with a fresh `.rebar` re-fired a sweep the store had just run"
    )


def test_the_lock_and_stamp_live_in_the_trackers_git_dir(tmp_path: Path) -> None:
    """On the store itself: beside the compaction intent journal, out of commits and status."""
    tracker = _git_store(tmp_path)
    git_dir = os.path.join(tracker, ".git")

    assert compact_trigger._trigger_lock_path(tracker) == os.path.join(
        git_dir, "compact-worker.lock"
    )
    assert os.path.dirname(compact_trigger._sweep_stamp_path(tracker)) == git_dir


def test_a_linked_worktree_tracker_keeps_them_in_its_own_git_dir(tmp_path: Path) -> None:
    """A local tracker is a linked worktree (``.git`` is a FILE): the sidecars follow the
    pointer to that worktree's git dir, which is per-store and outlives code worktrees."""
    base = Path(os.path.realpath(tmp_path)) / "repo"
    base.mkdir()
    for args in (
        ("git", "init", "-q"),
        ("git", "-c", "user.email=t@e.com", "-c", "user.name=t", "commit", "-q",
         "--allow-empty", "-m", "i"),
        ("git", "worktree", "add", "-q", "--detach", str(base / ".tickets-tracker")),
    ):  # fmt: skip
        subprocess.run(args, cwd=base, check=True, capture_output=True)
    tracker = str(base / ".tickets-tracker")
    git_dir = StorePaths(tracker).git_dir

    assert os.path.isdir(git_dir) and git_dir != os.path.join(tracker, ".git")
    assert os.path.dirname(compact_trigger._trigger_lock_path(tracker)) == git_dir
    compact_trigger.record_sweep(tracker)
    assert os.path.isfile(compact_trigger._sweep_stamp_path(tracker))
    assert not os.path.exists(os.path.join(base, ".rebar", "compact-sweep.stamp"))
