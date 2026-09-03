"""Snapshot helpers for reconciler tickets-branch drift checks."""

from __future__ import annotations

from pathlib import Path

# Sentinel returned by snapshot_head when the repository has no resolvable
# ref — i.e., neither a tickets branch nor any HEAD commit. Treated as a
# stable, never-equal-to-real-SHA value by drift-detection callers.
EMPTY_REPO_SENTINEL = "EMPTY_REPO"


def snapshot_head(repo_root: Path) -> str:
    """Return the current HEAD SHA of the tickets branch.

    Falls back to HEAD of the current branch when the tickets ref is absent
    (e.g., in a fresh test repo that has no orphan tickets branch yet).

    F9: a bare repository (``git init`` with no commits) has neither tickets
    nor a resolvable HEAD; the previous implementation called
    ``rev-parse HEAD`` with ``check=True`` and raised CalledProcessError,
    blocking reconciler bootstrap. We now return ``EMPTY_REPO_SENTINEL`` so
    callers can proceed and the drift guard simply treats every comparison
    as stable until the first commit lands.
    """
    from rebar.config import tickets_branch
    from rebar_reconciler import git_adapter

    branch = tickets_branch(repo_root)  # configured tracker.branch (default "tickets")
    result = git_adapter.rev_parse(repo_root, branch)
    if result.returncode != 0:
        result = git_adapter.rev_parse(repo_root, "HEAD")
        if result.returncode != 0:
            return EMPTY_REPO_SENTINEL
    return result.stdout.strip()
