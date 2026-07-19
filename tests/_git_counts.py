"""Shared test helper: crash-safe git commit counting.

``git rev-list --count <ref>`` can transiently fail under CI load (rc!=0 with
empty stdout — e.g. a background gc/pack race). The naive ``int(r.stdout.strip())``
turns that transient into an opaque ``ValueError: invalid literal for int() with
base 10: ''`` that masks git's real error and flakes whatever test called it
(bug efb7-09de, re-confirmed live on CI as cf3a). This helper is the single home
for the correct pattern: retry the transient, and on a persistent failure raise a
clear diagnostic that surfaces git's stderr instead of the opaque int('') crash.

Import it bare (``from _git_counts import commit_count``) like the other shared
root helpers — ``tests/`` is on ``sys.path`` (see tests/conftest.py).
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
