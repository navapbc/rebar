"""Shared primitives for the repo-isolation guard (see tests/conftest.py).

No test may commit to or dirty this checkout — tests operate on disposable
trackers under ``tmp_path``. The conftest guard is built on these two read-only
git probes; keeping them here lets the guard's self-test
(``tests/unit/test_repo_isolation_guard.py``) exercise the *same* logic against a
throwaway repo instead of a copy that could drift.
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Iterable
from types import SimpleNamespace

_run_process = subprocess.run
_stable_subprocess_time = SimpleNamespace(
    sleep=subprocess.time.sleep,  # type: ignore[attr-defined]
)

# Watch volatile repo-root state one level deep to catch test leaks into existing dirs.
# Git, tracker, and virtualenv dirs have legitimate churn and are excluded.
LEAK_WATCH_SUBDIRS = (".rebar",)
GIT_PROBE_TIMEOUT_SECONDS = 5

# Retry timed-out read-only Git probes without sleep. A starved sample is inconclusive.
# the last timeout still fails so persistent inability to sample cannot hide a HEAD move.
GIT_PROBE_ATTEMPTS = 3


def _git_probe_once(root, args: tuple[str, ...]) -> subprocess.CompletedProcess[str]:
    """One bounded attempt. Only the timeout is retryable — a non-zero exit is
    deterministic, so ``CalledProcessError`` propagates from the first attempt."""
    return _run_process(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        check=True,
        timeout=GIT_PROBE_TIMEOUT_SECONDS,
    )


def _run_git_probe(root, *args: str) -> subprocess.CompletedProcess[str]:
    """Run a bounded Git probe without inheriting a test's patched sleep.

    A timed-out attempt is re-sampled up to ``GIT_PROBE_ATTEMPTS`` times. The final
    attempt is deliberately OUTSIDE the retry loop so its ``TimeoutExpired`` propagates
    unconditionally — the loudness of a persistent failure is structural here, not a
    conditional that a later edit could quietly invert.
    """
    subprocess_time = subprocess.time  # type: ignore[attr-defined]
    subprocess.time = _stable_subprocess_time  # type: ignore[attr-defined]
    try:
        for _ in range(GIT_PROBE_ATTEMPTS - 1):
            try:
                return _git_probe_once(root, args)
            except subprocess.TimeoutExpired:
                continue
        return _git_probe_once(root, args)
    finally:
        subprocess.time = subprocess_time  # type: ignore[attr-defined]


def repo_leak_snapshot(root) -> set[str]:
    """Snapshot of the REPO_ROOT entries a test must not add to: every top-level
    name, PLUS the one-level entries inside the watched volatile state dirs (e.g.
    ``.rebar/run_snapshots``). Diff two snapshots (before/after a test) to find
    leaks — including a write INTO a pre-existing dir, which a top-level-only
    ``os.listdir`` diff cannot see."""
    snap = set(os.listdir(root))
    for sub in LEAK_WATCH_SUBDIRS:
        sub_dir = os.path.join(str(root), sub)
        if os.path.isdir(sub_dir):
            for entry in os.listdir(sub_dir):
                snap.add(f"{sub}/{entry}")
    return snap


def head(root) -> str | None:
    """The repo's current HEAD sha, or ``None`` if *root* is not a git repo."""
    try:
        return _run_git_probe(root, "rev-parse", "HEAD").stdout.strip()
    except subprocess.TimeoutExpired:
        raise
    except (OSError, subprocess.SubprocessError):
        return None


def porcelain(root) -> set[str] | None:
    """The set of ``git status --porcelain`` lines (gitignored paths excluded),
    or ``None`` if *root* is not a git repo. Compare two snapshots to find what a
    run added to the working tree."""
    try:
        out = _run_git_probe(root, "status", "--porcelain").stdout
    except subprocess.TimeoutExpired:
        raise
    except (OSError, subprocess.SubprocessError):
        return None
    return {line for line in out.splitlines() if line.strip()}


def leak_failure_message(leaked: Iterable[str]) -> str:
    """Describe a repo-root entry seen during a test without assigning its writer.

    The guard reports the entry without deleting it because another process may own it.
    """
    return (
        f"New entries appeared in REPO_ROOT during this test: {sorted(leaked)}\n"
        "They were left on disk untouched — the guard removes nothing, so clean "
        "up yourself once you know which cause applies.\n"
        "If this test created them, sandbox it — write under tmp_path (or "
        "another disposable temp dir) instead of the checkout.\n"
        "If it did not, a concurrent write from outside the suite looks "
        "identical here: a detached run shares this checkout with whatever "
        "else is touching it (an external commit, build, or editor)."
    )


def head_move_failure_message(before: str, after: str) -> str:
    """Describe a HEAD move without attributing it to the test or another process.

    Place the warning before the reset command because a reset can discard another writer's work.
    """
    return (
        f"The repo HEAD moved during this test ({before[:10]} -> {after[:10]}). "
        "The guard only samples HEAD around each test, so it cannot tell which "
        "process committed.\n"
        "If this test committed, isolate its git writes — pin "
        "GIT_CEILING_DIRECTORIES to the tmp root (see tests/scripts/graph/"
        "conftest.py::_isolate_git_from_enclosing_repo) or init the tracker as "
        "its own git repo.\n"
        "If it did not, a concurrent commit from outside the suite looks "
        "identical here: a detached run shares this checkout with whatever else "
        "is committing, rebasing, or switching branches in it.\n"
        "So do not run the recovery below until you know the moved HEAD is "
        "yours — it discards another process's commit and every "
        "uncommitted change in this worktree.\n"
        f"Once you own the commit(s), undo them with: git reset --hard {before[:10]}"
    )


def working_tree_failure_message(leaked: Iterable[str]) -> str:
    """Describe a session-wide working-tree change without assigning its writer.

    A test write and a concurrent editor, build, or commit can produce the same status entry.
    """
    entries = sorted(leaked)
    return (
        "REPO ISOLATION FAILURE: new changes appeared in the checkout during "
        "this test run. Entries from `git status --porcelain`:\n  "
        + "\n  ".join(entries[:40])
        + "\nThe backstop only diffs `git status --porcelain` around the whole "
        "session, so it cannot tell which process wrote them.\n"
        "If a test wrote them, sandbox it — write under tmp_path (or another "
        "disposable temp dir) instead of the checkout.\n"
        "If none did, a concurrent write from outside the suite looks identical "
        "here: a detached run shares this checkout with whatever else is "
        "committing, building, or saving in it."
    )
