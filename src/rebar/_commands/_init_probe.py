"""Bounded discovery of a ticket-store branch advertised by a git remote.

Init must distinguish an empty remote from a remote it cannot contact: creating an
orphan ``tickets`` branch in the latter case can permanently split ticket history.
This module owns that small network boundary, including a deliberately short-lived
cache so the best-effort central mount and the strict auto-init gate do not probe the
same remote twice in one CLI invocation.
"""

from __future__ import annotations

import os
import subprocess
import time
from collections.abc import Callable
from typing import Literal

from rebar._store.gitutil import run_git

RemoteBranchState = Literal["advertised", "absent", "unreachable"]

# ``fetch`` materializes a potentially large event history and retains its existing
# 300-second bound.  This is only a ref advertisement check, so it must fail much
# sooner instead of leaving automatic initialization stuck behind a dead network or
# credential helper.
REMOTE_PROBE_TIMEOUT = 10
REMOTE_PROBE_CACHE_TTL = 5.0
REMOTE_PROBE_CACHE_MAX = 128

ADVERTISED: RemoteBranchState = "advertised"
ABSENT: RemoteBranchState = "absent"
UNREACHABLE: RemoteBranchState = "unreachable"

_CACHE: dict[tuple[str, str, str], tuple[float, RemoteBranchState]] = {}


def _prune_cache(now: float) -> None:
    for key, (expires_at, _state) in list(_CACHE.items()):
        if now >= expires_at:
            _CACHE.pop(key, None)


def run_bounded_git(
    repo: str,
    *args: str,
    timeout: int,
    run_git_fn=run_git,
) -> subprocess.CompletedProcess:
    """Run one network-capable Git command with a descriptive timeout result.

    ``timeout`` bounds ELAPSED TIME, which is the wrong axis for a *stalled* transfer: a
    remote that opens the socket and then moves no bytes is indistinguishable from a slow
    cold clone, so it holds the caller for the whole budget (task 851e). A ``fetch`` — the
    long-budget command routed here, via ``init._git_fetch`` at the shared
    ``git_fetch.fetch_timeout`` cold-materialize backstop — therefore also gets
    the throughput-keyed abort from :func:`rebar._snapshot.git_fetch.stall_abort_args`,
    spliced ahead of the subcommand where git requires ``-c``. The ref-advertisement probe
    is left alone: its own 10s bound is already tighter than any low-speed window. The
    prefix is kept out of the timeout message below so it still names the operation the
    caller asked for."""
    prefix: tuple[str, ...] = ()
    if args and args[0] == "fetch":
        from rebar._snapshot.git_fetch import stall_abort_args

        prefix = tuple(stall_abort_args())
    try:
        return run_git_fn(repo, *prefix, *args, check=False, timeout=timeout)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            ["git", *args],
            124,
            "",
            f"git {' '.join(args)} timed out after {timeout}s",
        )


def remote_exists(repo: str, remote: str, *, run_git_fn=run_git) -> bool:
    """Return whether ``remote`` is configured locally.

    No remote URL means there is no remote branch to protect, so callers may safely
    take the normal greenfield path without making a network request.  A configured
    URL is intentionally not validated here; that is the bounded probe's job.
    """
    return run_git_fn(repo, "remote", "get-url", remote, check=False).returncode == 0


def require_s3_helper_if_s3_remote(repo: str, remote: str, *, run_git_fn=run_git) -> None:
    """Fail closed at init/mount if `remote` is an s3://|s3+zip:// URL lacking a current helper."""
    cp = run_git_fn(repo, "remote", "get-url", remote, check=False)
    if cp.returncode == 0:
        from rebar._store.push import _require_s3_helper_if_s3_url

        _require_s3_helper_if_s3_url(cp.stdout.strip())


def probe_remote_branch(
    repo: str,
    remote: str,
    branch: str,
    *,
    run_git_fn=run_git,
    monotonic_clock: Callable[[], float] | None = None,
) -> RemoteBranchState:
    """Classify a remote branch with one bounded ``git ls-remote`` call.

    ``0`` proves the branch is advertised, ``2`` proves the remote is reachable but
    lacks that branch, and every other result (including a timeout) is deliberately
    fail-closed as ``unreachable``.  ``monotonic_clock`` is injectable so expiry is
    deterministic in tests and never depends on wall-clock adjustments.
    """
    clock = monotonic_clock or time.monotonic
    key = (os.path.realpath(repo), remote, branch)
    now = clock()
    _prune_cache(now)
    cached = _CACHE.get(key)
    if cached is not None and now < cached[0]:
        return cached[1]

    ref = f"refs/heads/{branch}"
    result = run_bounded_git(
        repo,
        "ls-remote",
        "--exit-code",
        remote,
        ref,
        timeout=REMOTE_PROBE_TIMEOUT,
        run_git_fn=run_git_fn,
    )
    if result.returncode == 0:
        state = ADVERTISED
    elif result.returncode == 2:
        state = ABSENT
    else:
        state = UNREACHABLE
    if key not in _CACHE and len(_CACHE) >= REMOTE_PROBE_CACHE_MAX:
        oldest = min(_CACHE, key=lambda cache_key: _CACHE[cache_key][0])
        _CACHE.pop(oldest, None)
    _CACHE[key] = (clock() + REMOTE_PROBE_CACHE_TTL, state)
    return state


def remote_branch_unreachable(
    repo: str,
    remote: str,
    branch: str,
    *,
    has_ref: Callable[[str], bool],
    run_git_fn=run_git,
) -> bool:
    """Whether a missing local tracker must not silently become an orphan store.

    Callers provide their local-ref predicate because their git wrappers differ;
    the remote existence check and cached network classification remain centralized.
    A local branch or tracking ref is already safe to mount and therefore never
    counts as unreachable.
    """
    if has_ref(branch) or has_ref(f"{remote}/{branch}"):
        return False
    return remote_exists(repo, remote, run_git_fn=run_git_fn) and (
        probe_remote_branch(repo, remote, branch, run_git_fn=run_git_fn) == UNREACHABLE
    )


def clear_probe_cache() -> None:
    """Clear cached probe results for deterministic callers and focused tests."""
    _CACHE.clear()
