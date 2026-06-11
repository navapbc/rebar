"""Shared primitives for the repo-isolation guard (see tests/conftest.py).

No test may commit to or dirty this checkout — tests operate on disposable
trackers under ``tmp_path``. The conftest guard is built on these two read-only
git probes; keeping them here lets the guard's self-test
(``tests/unit/test_repo_isolation_guard.py``) exercise the *same* logic against a
throwaway repo instead of a copy that could drift.
"""

from __future__ import annotations

import subprocess


def head(root) -> str | None:
    """The repo's current HEAD sha, or ``None`` if *root* is not a git repo."""
    try:
        return subprocess.run(
            ["git", "-C", str(root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return None


def porcelain(root) -> set[str] | None:
    """The set of ``git status --porcelain`` lines (gitignored paths excluded),
    or ``None`` if *root* is not a git repo. Compare two snapshots to find what a
    run added to the working tree."""
    try:
        out = subprocess.run(
            ["git", "-C", str(root), "status", "--porcelain"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    return {line for line in out.splitlines() if line.strip()}
