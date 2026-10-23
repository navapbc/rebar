"""Count commits through git with a retry for transient rev-list failures.

Persistent failures report git stderr rather than raising an opaque int conversion error.
"""

from __future__ import annotations

import subprocess
import time
from pathlib import Path


def commit_count(
    git_dir: str | Path,
    ref: str = "HEAD",
    *,
    attempts: int = 5,
    delay: float = 0.05,
) -> int:
    """Return the number of commits reachable from *ref* in the repo at *git_dir*.

    Runs ``git -C <git_dir> rev-list --count <ref>``. A transient failure (rc!=0
    or empty stdout) is retried up to *attempts* times, sleeping *delay* seconds
    between tries. On a persistent failure it raises ``RuntimeError`` carrying
    git's rc/stdout/stderr — never a bare ``ValueError`` from ``int('')``.
    """
    git_dir = str(git_dir)
    last: subprocess.CompletedProcess[str] | None = None
    for attempt in range(attempts):
        r = subprocess.run(
            ["git", "-C", git_dir, "rev-list", "--count", ref],
            capture_output=True,
            text=True,
            check=False,
        )
        out = r.stdout.strip()
        if r.returncode == 0 and out:
            return int(out)
        last = r
        if attempt < attempts - 1:
            time.sleep(delay)
    assert last is not None  # attempts >= 1, so the loop always ran at least once
    raise RuntimeError(
        f"git rev-list --count {ref} failed after {attempts} attempts in {git_dir}: "
        f"rc={last.returncode} stdout={last.stdout.strip()!r} stderr={last.stderr.strip()!r}"
    )
