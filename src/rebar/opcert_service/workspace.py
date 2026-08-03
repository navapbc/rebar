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

#: Wall-clock bound (seconds) on every git subprocess in this module — :func:`_git` is its only
#: git seam, so this transitively bounds the two network fetches in :func:`_populate` too. An
#: unbounded git call blocks the worker forever on a stuck remote or a hung credential helper
#: (bug 747f measured a ~2.1-hour hang on such a path). 300s rather than the 30s used by
#: ``src/rebar/_store/push.py`` / ``src/rebar/_store/sync.py``: those bound an INCREMENTAL
#: ref-sized op against an already-warm clone, whereas this module fetches COLD into a fresh
#: ``mkdtemp`` and its fetches are not even shallow — 747f's "legitimately minutes on a cold
#: clone" profile, for which it adopted the same 300s bound.
_GIT_TIMEOUT = 300

#: Write-lock acquisition budget (seconds × attempts) for the boot-time ensure sweep in
#: :func:`_populate`. The review-bot's autodeploy health check is ``HEALTH_TIMEOUT=30``
#: (autodeploy config), so the sweep must not be able to spend the ``write_lock`` default of
#: 30s × 2 = 60s waiting on a contended lock — that alone can fail a deploy with no orphaned
#: lock involved (bug e43f, split out of castoff-tigerseye-ammonite). The sweep is idempotent
#: and re-runs on the next boot, so a contended lock is safely SKIPPED here rather than waited
#: out. Mirrors the MCP-boot budget in ``src/rebar/mcp_server.py`` (5s × 1).
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
    """Run one git command in ``cwd``, bounded by :data:`_GIT_TIMEOUT`.

    A ``subprocess.TimeoutExpired`` is neither an ``OSError`` nor a ``CalledProcessError`` and
    would bypass :func:`_git_ok`'s ``WorkspaceError`` conversion, so it is converted here — this
    is the module's only git seam."""
    try:
        return subprocess.run(
            ["git", "-C", cwd, *args],
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

    # Converge the freshly-mounted tracker into a writable rebar store (`.env-id` marker etc.),
    # mirroring reviewbot-ensure-tickets.sh. Idempotent — a no-op once converged. A SHORT
    # write-lock budget (bug e43f): this runs on the review-bot boot path behind a 30s deploy
    # health check, so a contended lock must SKIP the sweep (it re-runs next boot) rather than
    # burn write_lock's 60s default and fail the deploy on its own.
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
