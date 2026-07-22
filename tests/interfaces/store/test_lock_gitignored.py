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

import pytest

import rebar
from rebar._store.lock import MKDIR_LOCK_NAME, WRITE_LOCK_NAME
from rebar.graph._cache import _GRAPH_CACHE_FILE
from rebar.reducer import marker as marker_module


def _marker_names() -> tuple[str, str]:
    if not hasattr(marker_module, "ARCHIVE_MARKER_NAME") or not hasattr(
        marker_module, "MARKER_LOCK_NAME"
    ):
        pytest.fail("per-ticket marker name constants are absent")
    return marker_module.ARCHIVE_MARKER_NAME, marker_module.MARKER_LOCK_NAME


def _git_out(*args: str, cwd: Path) -> str:
    return subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True).stdout


def test_seeded_gitignore_covers_lock_and_graph_cache(rebar_repo: Path) -> None:
    archive_marker, marker_lock = _marker_names()
    tracker = rebar_repo / ".tickets-tracker"
    lines = _git_out("show", "tickets:.gitignore", cwd=tracker).splitlines()

    # Exact full-line matches (parity with the existing .cache.json assertion).
    assert WRITE_LOCK_NAME in lines, f"{WRITE_LOCK_NAME} not gitignored: {lines}"
    assert f"{MKDIR_LOCK_NAME}/" in lines, f"{MKDIR_LOCK_NAME}/ not gitignored: {lines}"
    assert _GRAPH_CACHE_FILE in lines, f"{_GRAPH_CACHE_FILE} not gitignored: {lines}"
    assert f"*/{archive_marker}" in lines
    assert f"*/{marker_lock}" in lines


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


def test_archiving_leaves_only_ignored_per_ticket_markers(rebar_repo: Path) -> None:
    archive_marker, marker_lock = _marker_names()
    tracker = rebar_repo / ".tickets-tracker"
    tid = rebar.create_ticket("task", "archive marker ignore test", repo_root=str(rebar_repo))

    rebar.archive(tid, repo_root=str(rebar_repo))

    assert (tracker / tid / archive_marker).exists()
    assert (tracker / tid / marker_lock).exists()
    porcelain = _git_out("status", "--porcelain", cwd=tracker).splitlines()
    assert [line for line in porcelain if line.startswith("??")] == []
