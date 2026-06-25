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


# Volatile REPO_ROOT state dirs a test must never write into, but which exist
# locally because this checkout dogfoods rebar (so ``.rebar/`` is always present).
# Watched ONE level deep so a leak INTO them is caught locally — not only on a
# fresh CI checkout where the same write would instead create a new top-level
# entry (the false-green/false-red split behind bug hurt-brow-swan). Kept shallow
# (one extra ``os.listdir`` each) so the per-test guard stays cheap; dirs with
# legitimate per-test churn (``.git/``, ``.tickets-tracker/``, ``.venv/``) are
# deliberately NOT watched.
LEAK_WATCH_SUBDIRS = (".rebar",)


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
