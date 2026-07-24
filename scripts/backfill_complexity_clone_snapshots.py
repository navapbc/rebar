"""One-time backfill of complexity/clone snapshots at sampled historical commits.

For each sampled commit this script measures cyclomatic complexity and the clone
count of the source tree at that commit and writes a metrics snapshot (via
``src/rebar/metrics/snapshot.py``) tagged with the *commit's* committer date — so
the resulting series is a true historical trend rather than a stack of points at
the run's wall-clock time.

The tool seam ``measure_tree`` is a module-level function so tests can monkeypatch
it without lizard/jscpd installed.
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
from pathlib import Path

from rebar.metrics import snapshot
from rebar.metrics.analyzers._jscpd import run_jscpd

logger = logging.getLogger(__name__)


def measure_tree(tree_path: str | Path) -> dict | None:
    """Measure complexity + clone count of the source tree at ``tree_path``.

    Shells to ``lizard`` (summing cyclomatic complexity across all functions) and
    ``jscpd`` (reading ``statistics.total.clones`` from its JSON report). Returns
    ``{"complexity": int, "clone_count": int}`` or ``None`` if either tool is
    absent or errors.
    """
    tree = str(tree_path)
    try:
        complexity = _measure_complexity(tree)
        clone_count = _measure_clone_count(tree)
    except (OSError, subprocess.SubprocessError, ValueError, KeyError, json.JSONDecodeError):
        return None
    if complexity is None or clone_count is None:
        return None
    return {"complexity": complexity, "clone_count": clone_count}


def _measure_complexity(tree: str) -> int | None:
    """Sum cyclomatic complexity across all functions reported by ``lizard``."""
    proc = subprocess.run(
        ["lizard", tree],
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode not in (0, 1):
        return None
    total = 0
    seen = False
    for line in proc.stdout.splitlines():
        parts = line.split()
        # lizard function rows begin with: NLOC CCN token PARAM length location...
        if len(parts) >= 5 and parts[0].isdigit() and parts[1].isdigit():
            total += int(parts[1])
            seen = True
    return total if seen or proc.returncode == 0 else None


def _measure_clone_count(tree: str) -> int | None:
    """Read ``statistics.total.clones`` from a ``jscpd`` JSON report."""
    return int(run_jscpd(tree)["clones"])


def _commit_date(repo_root: str | Path, sha: str) -> str:
    """Return the committer date of ``sha`` as an ISO-8601 string."""
    proc = subprocess.run(
        ["git", "show", "-s", "--format=%cI", sha],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout.strip()


def backfill(repo_root: str | Path, commits: list[str]) -> int:
    """Write a snapshot for each measurable commit in ``commits``.

    For each sha: resolve its committer date, measure the tree via ``measure_tree``
    (skip, non-fatally, when it returns ``None``), and append a snapshot stamped
    with the commit's ISO date. Returns the number of snapshots written.
    """
    written = 0
    for sha in commits:
        ts = _commit_date(repo_root, sha)
        measured = measure_tree(repo_root)
        if measured is None:
            logger.info("skipping %s: measure_tree returned None", sha)
            continue
        record = {
            "complexity": measured["complexity"],
            "clone_count": measured["clone_count"],
            "source": "snapshot",
            "confidence": "high",
        }
        snapshot.write_snapshot(record, repo_root=repo_root, ts=ts)
        written += 1
    return written


def _sample_commits(repo_root: str | Path, count: int) -> list[str]:
    proc = subprocess.run(
        ["git", "log", "--format=%H", f"-n{count}"],
        cwd=str(repo_root),
        capture_output=True,
        text=True,
        check=True,
    )
    return [line for line in proc.stdout.splitlines() if line]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", default=".", help="repository root to snapshot")
    parser.add_argument("--count", type=int, default=10, help="number of recent commits to sample")
    parser.add_argument(
        "commits", nargs="*", help="explicit commit shas (defaults to recent sample)"
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(message)s")

    commits = args.commits or _sample_commits(args.repo_root, args.count)
    written = backfill(args.repo_root, commits)
    logger.info("wrote %d snapshot(s)", written)
    return 0


if __name__ == "__main__":
    sys.exit(main())
