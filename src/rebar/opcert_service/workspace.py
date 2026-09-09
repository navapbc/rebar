"""Disposable authoritative workspace for a trusted op-cert job.

The worker fetches review ``main`` and the tickets branch itself, mounts the
tracker, and signs against that state rather than client input. Pushes are
disabled and all remotes are removed, so signature events remain in the
discarded clone.
"""

from __future__ import annotations

import subprocess
import tempfile
from dataclasses import dataclass

from rebar import config as _config
from rebar._snapshot.git_fetch import stall_abort_args
from rebar.opcert_service.config import OpcertServiceConfig

#: Bound every Git call through the module's sole seam. Cold full fetches may
#: legitimately take minutes, hence 300 seconds. Transport stall detection aborts
#: dead connections sooner.
_GIT_TIMEOUT = 300

#: Boot-time ensures get one five-second lock attempt, keeping deployment health
#: checks below 30 seconds. A skipped idempotent sweep runs again next boot.
_ENSURE_BOOT_TIMEOUT = 5
_ENSURE_BOOT_ATTEMPTS = 1


class WorkspaceError(Exception):
    """A workspace could not be prepared (a git/clone/fetch failure). Maps to an internal job
    error — the client cannot cause it and there is nothing to sign."""


@dataclass
class Workspace:
    """A prepared ephemeral workspace: ``repo_root`` (the code checkout, also the rebar repo root)
    and ``merged_log_commit`` (the fetched review-remote ``main`` tip sha)."""

    repo_root: str
    merged_log_commit: str


# raw-git-ok: disposable sandbox repo, not the tracker
def _git(cwd: str, *args: str) -> subprocess.CompletedProcess:
    """Run bounded Git in ``cwd``.

    Convert timeouts to :class:`WorkspaceError`. Fetches also receive throughput
    stall guards. Local Git operations do not."""
    prefix = stall_abort_args() if args and args[0] == "fetch" else []
    try:
        return subprocess.run(
            # The -c pairs must precede the subcommand; see stall_abort_args().
            ["git", "-C", cwd, *prefix, *args],
            capture_output=True,
            text=True,
            check=False,
            timeout=_GIT_TIMEOUT,
        )
    except subprocess.TimeoutExpired as exc:
        op = args[0] if args else "git"
        raise WorkspaceError(
            f"git {op} timed out after {_GIT_TIMEOUT} seconds: {' '.join(args)}"
        ) from exc


# raw-git-ok: disposable sandbox repo, not the tracker
def _git_ok(cwd: str, *args: str) -> None:
    proc = _git(cwd, *args)
    if proc.returncode != 0:
        raise WorkspaceError(f"git {' '.join(args)} failed: {proc.stderr.strip()}")


def prepare_workspace(cfg: OpcertServiceConfig) -> Workspace:
    """Clone + fetch authoritative state into a fresh temp dir; return the :class:`Workspace`.

    The caller MUST :func:`discard` it when the job finishes (the service does so in a ``finally``).
    """
    if not cfg.review_remote_url or not cfg.tickets_remote_url:
        raise WorkspaceError(
            "REBAR_OPCERT_REVIEW_REMOTE_URL and REBAR_OPCERT_TICKETS_REMOTE_URL must be set"
        )
    root = tempfile.mkdtemp(prefix="rebar-opcert-ws-")
    try:
        return _populate(root, cfg)
    except Exception:
        discard(root)
        raise


def _populate(root: str, cfg: OpcertServiceConfig) -> Workspace:
    # Guaranteed non-None by prepare_workspace's guard; narrow for the type checker.
    assert cfg.review_remote_url is not None and cfg.tickets_remote_url is not None
    # 1. Code: clone the review remote and check out its `main` tip.
    _git_ok(root, "init", "-q")
    _git_ok(root, "remote", "add", "review", cfg.review_remote_url)
    _git_ok(root, "fetch", "--quiet", "review", cfg.review_branch)
    _git_ok(root, "checkout", "-q", "-B", cfg.review_branch, f"review/{cfg.review_branch}")
    head = _git(root, "rev-parse", "HEAD")
    if head.returncode != 0 or not head.stdout.strip():
        raise WorkspaceError("could not resolve the review remote's main HEAD")
    merged_log_commit = head.stdout.strip()

    # A repo-local identity so rebar's SIGNATURE-event commit succeeds in the ephemeral clone.
    _git_ok(root, "config", "user.email", "opcert@rebar.invalid")
    _git_ok(root, "config", "user.name", "rebar-opcert")
    _git_ok(root, "config", "commit.gpgsign", "false")

    # 2. Tickets: fetch the tickets branch from the tickets remote and mount it as the tracker
    #    worktree rebar reads/writes ticket state through.
    branch = _config.tickets_branch(root)  # configured tracker.branch (default "tickets")
    tracker = str(_config.tracker_dir(root))
    _git_ok(root, "remote", "add", "tickets", cfg.tickets_remote_url)
    _git_ok(root, "fetch", "--quiet", "tickets", branch)
    _git_ok(root, "worktree", "add", "-q", "-B", branch, tracker, f"tickets/{branch}")
    _git_ok(tracker, "config", "user.email", "opcert@rebar.invalid")
    _git_ok(tracker, "config", "user.name", "rebar-opcert")
    _git_ok(tracker, "config", "commit.gpgsign", "false")

    # Converge the mounted tracker with a short boot lock budget. The idempotent
    # sweep may skip contention and retry on the next boot.
    from rebar._store.ensures import run_ensures

    for _ in run_ensures(tracker, timeout=_ENSURE_BOOT_TIMEOUT, attempts=_ENSURE_BOOT_ATTEMPTS):
        pass

    # 3. Store-read-only: strip EVERY remote so there is no push target/credential, defense in
    #    depth alongside REBAR_SYNC_PUSH=off (set by the worker). The server never pushes.
    for remote in ("review", "tickets"):
        _git(root, "remote", "remove", remote)

    return Workspace(repo_root=root, merged_log_commit=merged_log_commit)


def discard(root: str) -> None:
    """Remove the ephemeral workspace (its git worktrees + the whole tree). Best-effort."""
    import shutil

    # Prune the linked tracker worktree registration first so nothing dangles, then rmtree.
    _git(root, "worktree", "prune")
    shutil.rmtree(root, ignore_errors=True)
