"""Read-only ADR 0106 remote-anchored reclamation eligibility predicates."""

from __future__ import annotations

import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

from rebar import config
from rebar._store.gitutil import run_git

_GIT_READ_TIMEOUT_S = 30.0


def checkpoint_commit_eligible(
    repo_root: str | os.PathLike[str],
    checkpoint_commit: str,
    *,
    now: datetime | None = None,
) -> bool:
    """Whether a collapse checkpoint commit satisfies ADR 0106's remote horizon.

    The predicate is deliberately read-only: it inspects the local remote-tracking
    tickets ref for the configured sync remote and never fetches, pushes, updates refs,
    compacts, deletes, or rewrites history.
    """
    root = Path(repo_root)
    remote_tip = _resolve_remote_tip(root)
    commit = _resolve_commit(root, checkpoint_commit)
    if remote_tip is None or commit is None:
        return False
    if not _is_ancestor(root, commit, remote_tip):
        return False
    commit_time = _commit_time(root, commit)
    if commit_time is None:
        return False
    horizon = timedelta(days=config.reclaim_horizon_days(root))
    return (_coerce_now(now) - commit_time) >= horizon


def retired_tombstone_eligible(
    repo_root: str | os.PathLike[str],
    retired_path: str | os.PathLike[str],
    *,
    now: datetime | None = None,
) -> bool:
    """Whether a ``*.retired`` tombstone's folding commit satisfies the horizon."""
    path = Path(retired_path).as_posix()
    if not path.endswith(".retired"):
        return False
    folding_commit = _tombstone_folding_commit(Path(repo_root), path)
    if folding_commit is None:
        return False
    return checkpoint_commit_eligible(repo_root, folding_commit, now=now)


def _coerce_now(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(UTC)
    if now.tzinfo is None:
        return now.replace(tzinfo=UTC)
    return now.astimezone(UTC)


def _resolve_remote_tip(root: Path) -> str | None:
    remote = config.tickets_remote(root)
    branch = config.tickets_branch(root)
    return _resolve_commit(root, f"refs/remotes/{remote}/{branch}")


def _resolve_commit(root: Path, rev: str) -> str | None:
    result = run_git(
        root,
        "rev-parse",
        "--verify",
        f"{rev}^{{commit}}",
        check=False,
        timeout=_GIT_READ_TIMEOUT_S,
    )
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def _is_ancestor(root: Path, ancestor: str, descendant: str) -> bool:
    result = run_git(
        root,
        "merge-base",
        "--is-ancestor",
        ancestor,
        descendant,
        check=False,
        timeout=_GIT_READ_TIMEOUT_S,
    )
    return result.returncode == 0


def _commit_time(root: Path, commit: str) -> datetime | None:
    result = run_git(
        root,
        "show",
        "-s",
        "--format=%ct",
        commit,
        check=False,
        timeout=_GIT_READ_TIMEOUT_S,
    )
    if result.returncode != 0:
        return None
    try:
        seconds = int(result.stdout.strip())
    except ValueError:
        return None
    return datetime.fromtimestamp(seconds, UTC)


def _tombstone_folding_commit(root: Path, path: str) -> str | None:
    result = run_git(
        root,
        "log",
        "--diff-filter=A",
        "--format=%H",
        "--",
        path,
        check=False,
        timeout=_GIT_READ_TIMEOUT_S,
    )
    if result.returncode != 0:
        return None
    commits = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    return commits[0] if commits else None
