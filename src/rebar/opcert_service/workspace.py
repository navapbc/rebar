"""Disposable authoritative workspace for a trusted op-cert job.

The worker fetches review ``main`` and the tickets branch itself, mounts the
tracker, and signs against that state rather than client input. Pushes are
disabled and refs are fetched by URL, so the clone never has a remote and
signature events remain in the discarded clone.
"""

from __future__ import annotations

import os
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
def _git(cwd: str, *args: str, shown: str | None = None) -> subprocess.CompletedProcess:
    """Run bounded Git in ``cwd``.

    Convert timeouts to :class:`WorkspaceError`, described by ``shown`` when given
    (so a fetch URL never reaches the job's error). Fetches also receive throughput
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
            f"git {op} timed out after {_GIT_TIMEOUT} seconds: {shown or ' '.join(args)}"
        ) from exc


# raw-git-ok: disposable sandbox repo, not the tracker
def _git_ok(cwd: str, *args: str, shown: str | None = None) -> None:
    proc = _git(cwd, *args, shown=shown)
    if proc.returncode != 0:
        raise WorkspaceError(f"git {shown or ' '.join(args)} failed: {proc.stderr.strip()}")


def _fetch(root: str, role: str, url: str, ref: str) -> None:
    """Fetch ``ref`` from ``url`` into ``FETCH_HEAD`` without configuring a remote.

    Errors name the remote's ``role``, not its URL, which may carry credentials."""
    _git_ok(root, "fetch", "--quiet", url, ref, shown=f"fetch --quiet <{role} remote> {ref}")


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


#: Repo-local identity so rebar's SIGNATURE-event commit succeeds in the ephemeral clone.
#: Written once into the new repo's config; the linked tracker worktree shares it.
_LOCAL_IDENTITY = (
    "[user]\n\temail = opcert@rebar.invalid\n\tname = rebar-opcert\n[commit]\n\tgpgsign = false\n"
)


def _write_local_identity(root: str) -> None:
    """Append :data:`_LOCAL_IDENTITY` to the repo config in one write, not 6 git calls."""
    try:
        with open(os.path.join(root, ".git", "config"), "a", encoding="utf-8") as fh:
            fh.write(_LOCAL_IDENTITY)
    except OSError as exc:
        raise WorkspaceError(f"could not write the workspace git identity: {exc}") from exc


def _populate(root: str, cfg: OpcertServiceConfig) -> Workspace:
    # Guaranteed non-None by prepare_workspace's guard; narrow for the type checker.
    assert cfg.review_remote_url is not None and cfg.tickets_remote_url is not None
    # Each git start costs up to a second on a loaded host, so only the calls that do
    # work run (bug upset-prissy-olm). Refs are fetched by URL into FETCH_HEAD, so no
    # remote, remote-tracking ref or upstream is ever configured: the server has no
    # push target or credential (defense in depth alongside REBAR_SYNC_PUSH=off).
    _git_ok(root, "init", "-q")

    # 1. Code: check out the review remote's `main` tip.
    _fetch(root, "review", cfg.review_remote_url, cfg.review_branch)
    _git_ok(root, "checkout", "-q", "-B", cfg.review_branch, "FETCH_HEAD")
    head = _git(root, "rev-parse", "HEAD")
    if head.returncode != 0 or not head.stdout.strip():
        raise WorkspaceError("could not resolve the review remote's main HEAD")
    merged_log_commit = head.stdout.strip()
    _write_local_identity(root)

    # 2. Tickets: fetch the tickets branch from the tickets remote and mount it as the tracker
    #    worktree rebar reads/writes ticket state through.
    branch = _config.tickets_branch(root)  # configured tracker.branch (default "tickets")
    tracker = str(_config.tracker_dir(root))
    _fetch(root, "tickets", cfg.tickets_remote_url, branch)
    _git_ok(root, "worktree", "add", "-q", "-B", branch, tracker, "FETCH_HEAD")

    # Converge the mounted tracker with a short boot lock budget. The idempotent
    # sweep may skip contention and retry on the next boot.
    from rebar._store.ensures import run_ensures

    for _ in run_ensures(tracker, timeout=_ENSURE_BOOT_TIMEOUT, attempts=_ENSURE_BOOT_ATTEMPTS):
        pass

    return Workspace(repo_root=root, merged_log_commit=merged_log_commit)


def discard(root: str) -> None:
    """Remove the ephemeral workspace (its git worktrees + the whole tree). Best-effort.

    The linked tracker's registration lives under ``<root>/.git/worktrees``, so the
    one ``rmtree`` removes it with the tree; no ``git worktree prune`` is needed."""
    import shutil

    shutil.rmtree(root, ignore_errors=True)
