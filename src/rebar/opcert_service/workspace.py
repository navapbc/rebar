"""Ephemeral authoritative-state workspace for a trusted op-cert gate job (story ee0b).

The load-bearing security property: the worker fetches authoritative state ITSELF — never trusts
the client. It clones the review remote's code, checks out its ``main`` tip (that sha becomes the
signed ``merged_log_commit``), and mounts the tickets branch from the tickets remote as a rebar
tracker worktree, so ``review_plan`` / ``verify_completion`` read state the client cannot influence.

Store-read-only: the workspace runs with ``REBAR_SYNC_PUSH=off`` AND every git remote removed, so a
gate's ``sign=True`` SIGNATURE append lands ONLY in this discarded clone — never on the shared
tickets branch. The workspace is deleted after the job.
"""

from __future__ import annotations

import subprocess
import tempfile
from dataclasses import dataclass

from rebar import config as _config
from rebar.opcert_service.config import OpcertServiceConfig


class WorkspaceError(Exception):
    """A workspace could not be prepared (a git/clone/fetch failure). Maps to an internal job
    error — the client cannot cause it and there is nothing to sign."""


@dataclass
class Workspace:
    """A prepared ephemeral workspace: ``repo_root`` (the code checkout, also the rebar repo root)
    and ``merged_log_commit`` (the fetched review-remote ``main`` tip sha)."""

    repo_root: str
    merged_log_commit: str


def _git(cwd: str, *args: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True, check=False)


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

    # Converge the freshly-mounted tracker into a writable rebar store (`.env-id` marker etc.),
    # mirroring reviewbot-ensure-tickets.sh. Idempotent — a no-op once converged.
    from rebar._store.ensures import run_ensures

    for _ in run_ensures(tracker):
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
