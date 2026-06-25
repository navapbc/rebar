"""Runtime write/cache artifacts must be gitignored in the tracker worktree.

Regression for bug ``stem-ewe-tomb``: the flock write-lock file
(``.ticket-write.lock``) and the graph compile cache (``.graph-cache.json``) are
created at the tracker root during normal operation and the lock file is
intentionally *not* unlinked on release, so without a ``.gitignore`` entry they
surface as untracked files in the ``.tickets-tracker`` worktree — noising up
``git status`` and tripping cleanliness checks / stop hooks.

The seeded tracker ``.gitignore`` must ignore these artifacts, and the ignore
entries must stay consistent with the defining name constants (no drifting
literals).
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import rebar
from rebar._store.lock import MKDIR_LOCK_NAME, WRITE_LOCK_NAME
from rebar.graph._cache import _GRAPH_CACHE_FILE


def _git_out(*args: str, cwd: Path) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True).stdout


def test_seeded_gitignore_covers_lock_and_graph_cache(rebar_repo: Path) -> None:
    tracker = rebar_repo / ".tickets-tracker"
    lines = _git_out("show", "tickets:.gitignore", cwd=tracker).splitlines()

    # Exact full-line matches (parity with the existing .cache.json assertion).
    assert WRITE_LOCK_NAME in lines, f"{WRITE_LOCK_NAME} not gitignored: {lines}"
    assert f"{MKDIR_LOCK_NAME}/" in lines, f"{MKDIR_LOCK_NAME}/ not gitignored: {lines}"
    assert _GRAPH_CACHE_FILE in lines, f"{_GRAPH_CACHE_FILE} not gitignored: {lines}"


def test_no_untracked_runtime_artifacts_after_write_and_compile(rebar_repo: Path) -> None:
    tracker = rebar_repo / ".tickets-tracker"

    # A write (takes the flock lock -> creates .ticket-write.lock, never unlinked)...
    tid = rebar.create_ticket("task", "lock gitignore test", repo_root=str(rebar_repo))
    rebar.comment(tid, "another write", repo_root=str(rebar_repo))
    # ...and a graph compile (writes .graph-cache.json at the tracker root).
    rebar.deps(tid, repo_root=str(rebar_repo))

    # The lock file must actually exist on disk (proves the artifact is real)...
    assert (tracker / WRITE_LOCK_NAME).exists(), "write-lock file was not created"

    # ...yet the tracker worktree must report a clean status (no untracked noise).
    porcelain = _git_out("status", "--porcelain", cwd=tracker).splitlines()
    untracked = [ln for ln in porcelain if ln.startswith("??")]
    assert untracked == [], f"untracked runtime artifacts in tracker: {untracked}"
