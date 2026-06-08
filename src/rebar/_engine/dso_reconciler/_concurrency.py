"""Concurrency primitives for tickets-branch writes.

Provides snapshot isolation (snapshot_head) and rebase-retry semantics
(rebase_retry) for reconciler passes that write to the tickets orphan branch.

This module is inert on its own; callers are wired by subsequent tasks.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable


@dataclass
class ConcurrencyEvent:
    """Structured event emitted by rebase_retry to describe a non-OK outcome."""

    kind: str  # abort_due_to_drift | reject_and_reschedule | abort_due_to_error
    message: str = ""
    attempt: int = 0


@dataclass
class Result:
    """Return value from rebase_retry."""

    ok: bool
    event: ConcurrencyEvent | None = None
    value: Any = None


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
    result = subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", "tickets"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        result = subprocess.run(
            ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode != 0:
            return EMPTY_REPO_SENTINEL
    return result.stdout.strip()


def rebase_retry(
    repo_root: Path,
    write_fn: Callable[[], Any],
    *,
    max_attempts: int = 3,
) -> Result:
    """Execute write_fn with rebase-retry on HEAD drift.

    Algorithm for each attempt:
      1. Capture tickets-branch HEAD before write.
      2. Execute write_fn().
      3. If write_fn raises an exception → fail fast with abort_due_to_error
         (errors are not retried).
      4. If HEAD changed since capture → retry (drift indicates a concurrent
         writer; the next attempt rebases by re-pinning the new HEAD).
      5. Otherwise return Result(ok=True, value=<write_fn return value>).

    When all ``max_attempts`` end in drift → reject_and_reschedule. The
    Result.event.attempt then equals ``max_attempts``.

    No persistent state is held across passes; all counters live in local
    stack frames.
    """
    last_drift_message = ""
    for attempt in range(1, max_attempts + 1):
        head_before = snapshot_head(repo_root)
        try:
            value = write_fn()
        except Exception as exc:  # noqa: BLE001
            # Errors fail fast — do not retry.
            return Result(
                ok=False,
                event=ConcurrencyEvent(
                    kind="abort_due_to_error",
                    message=str(exc),
                    attempt=attempt,
                ),
            )
        head_after = snapshot_head(repo_root)
        if head_after != head_before:
            # Drift detected — concurrent writer; loop to retry this attempt.
            last_drift_message = (
                f"HEAD changed {head_before[:8]}->{head_after[:8]}"
            )
            continue
        return Result(ok=True, value=value)

    # All attempts exhausted by drift → reject and reschedule.
    msg = f"exhausted {max_attempts} attempts"
    if last_drift_message:
        msg = f"{msg}; last drift: {last_drift_message}"
    return Result(
        ok=False,
        event=ConcurrencyEvent(
            kind="reject_and_reschedule",
            message=msg,
            attempt=max_attempts,
        ),
    )
