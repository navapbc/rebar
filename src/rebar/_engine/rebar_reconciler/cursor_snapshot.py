#!/usr/bin/env python3
"""Cursor snapshot step: captures outbound and inbound cursors to bridge_state/cursor-snapshot.json.

The single-file public entry point is `run(repo_root)`. The function is
intentionally a thin orchestrator over four private helpers, one per logical
step, so each step can be tested in isolation with mocked subprocess calls.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class StepResult:
    name: str
    ok: bool
    message: str
    details: dict = field(default_factory=dict)


# Default per-subprocess timeout. Any git subprocess that hangs past this
# returns control to the orchestrator's TimeoutExpired handler so the
# cutover-prep sequence never blocks indefinitely on a stuck git operation.
_GIT_TIMEOUT_S = 30


# ── Step 1: Resolve tickets-branch HEAD ──────────────────────────────────────


def _resolve_tickets_head(repo_root: Path) -> tuple[str | None, str | None]:
    """Return (head_sha, error_message). Exactly one is non-None."""
    result = subprocess.run(
        ["git", "rev-parse", "tickets"],
        capture_output=True,
        text=True,
        cwd=str(repo_root),
        check=False,
        timeout=_GIT_TIMEOUT_S,
    )
    if result.returncode != 0:
        return None, f"could not resolve tickets branch: {result.stderr.strip()}"
    return result.stdout.strip(), None


# ── Step 3+4: Load outbound checkpoint and inbound cursor ────────────────────


def _load_cursors(repo_root: Path) -> tuple[dict | None, dict | None]:
    """Return (outbound_checkpoint, inbound_cursor). Each may be None when
    the source artifact is absent or malformed — both are best-effort."""
    outbound: dict | None = None
    cp_result = subprocess.run(
        ["git", "show", "tickets:.outbound-checkpoint.json"],
        capture_output=True,
        text=True,
        cwd=str(repo_root),
        check=False,
        timeout=_GIT_TIMEOUT_S,
    )
    if cp_result.returncode == 0:
        try:
            outbound = json.loads(cp_result.stdout)
        except json.JSONDecodeError:
            outbound = None

    inbound: dict | None = None
    inbound_path = repo_root / "bridge_state" / "inbound-cursor.json"
    if inbound_path.exists():
        # Catch OSError (permission/IO transient) and UnicodeDecodeError
        # (binary garbage) alongside JSONDecodeError — _load_cursors is
        # documented as best-effort, so ANY read failure degrades to inbound=None
        # rather than aborting the entire snapshot step.
        try:
            inbound = json.loads(inbound_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError, UnicodeDecodeError):
            inbound = None

    return outbound, inbound


# ── Step 6: Atomic write via tempfile ────────────────────────────────────────


def _write_snapshot_atomically(snapshot_path: Path, snapshot: dict) -> None:
    """Write `snapshot` to `snapshot_path` atomically.

    The write goes through a tempfile in the same directory + `os.replace`
    so a crash mid-write cannot leave the destination half-overwritten.
    `os.replace` (not `os.rename`) is required for cross-platform atomic
    overwrite semantics — `os.rename` fails on Windows when the destination
    exists.
    """
    snapshot_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path: Path | None = None
    try:
        fd, tmp_path_str = tempfile.mkstemp(
            dir=str(snapshot_path.parent),
            prefix=".cursor-snapshot-tmp.",
        )
        tmp_path = Path(tmp_path_str)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(snapshot, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, snapshot_path)
        tmp_path = None
    finally:
        if tmp_path and tmp_path.exists():
            tmp_path.unlink()


# ── Step 7: Commit to tickets orphan branch ──────────────────────────────────


def _current_branch(repo_root: Path) -> str:
    """Return the current branch name in repo_root.

    Returns ``"(detached)"`` for both detached-HEAD state and rev-parse
    failures, so callers can distinguish "no branch" from "tickets" or any
    other named branch by an explicit string rather than by an empty value.
    Surfacing ``""`` was ambiguous — operators couldn't tell whether the
    subprocess crashed or HEAD was simply detached.
    """
    result = subprocess.run(
        ["git", "rev-parse", "--abbrev-ref", "HEAD"],
        capture_output=True,
        text=True,
        cwd=str(repo_root),
        check=False,
        timeout=_GIT_TIMEOUT_S,
    )
    if result.returncode != 0:
        return "(detached)"
    branch = result.stdout.strip()
    return "(detached)" if branch in ("", "HEAD") else branch


def _commit_to_tickets_branch(
    repo_root: Path, snapshot_path: Path, head_sha: str
) -> tuple[bool, bool, str, str]:
    """Stage and commit the snapshot to the tickets branch.

    Returns ``(ok, committed, branch, message)``:
      * ``ok``         — step succeeded (no error). True for both the
                         committed and skipped-on-non-tickets paths.
      * ``committed``  — whether `git commit` actually ran (and a commit
                         was either created or the tree was already clean
                         per the "nothing to commit" idempotency rule).
                         False when the step was skipped because the current
                         branch is not 'tickets'.
      * ``branch``     — the branch name observed at decision time, returned
                         so callers can reuse it instead of re-spawning
                         another `git rev-parse --abbrev-ref HEAD` subprocess.
      * ``message``    — human-readable status for the StepResult message.

    The commit step is only safe when the working tree at ``repo_root`` is
    actually on the ``tickets`` branch; otherwise `git add`/`git commit`
    would land the snapshot on the wrong branch (typically ``main`` or a
    feature branch) — directly contradicting the "commit to tickets orphan
    branch" intent.

    When ``repo_root`` is on a non-tickets branch we treat this step as a
    safe no-op: the snapshot file is already on disk via
    ``_write_snapshot_atomically`` (the durable artifact the cutover-prep needs);
    we skip the commit and surface the skip in the message so operators
    can route the commit to the correct worktree out of band. Callers MUST
    branch on ``committed`` (not just ``ok``) when downstream consumers
    require a real commit on the tickets branch.
    """
    branch = _current_branch(repo_root)
    if branch != "tickets":
        return (
            True,
            False,
            branch,
            f"snapshot written; commit step skipped (current branch={branch!r}, not 'tickets')",
        )

    git_add = subprocess.run(
        ["git", "add", str(snapshot_path)],
        capture_output=True,
        text=True,
        cwd=str(repo_root),
        check=False,
        timeout=_GIT_TIMEOUT_S,
    )
    if git_add.returncode != 0:
        return False, False, branch, f"git add failed: {git_add.stderr.strip()}"

    # TOCTOU defense: re-check the branch immediately before commit. If
    # another process switched branches between our first _current_branch()
    # check and now, abort rather than land the snapshot on the wrong branch.
    # The git index stage we just made is left in place — `git reset` would
    # mask the race; operators investigating an unexpected committed=False
    # benefit from seeing the staged file.
    branch_now = _current_branch(repo_root)
    if branch_now != "tickets":
        return (
            True,
            False,
            branch_now,
            f"snapshot written; commit aborted — branch switched mid-step "
            f"(was 'tickets', now {branch_now!r})",
        )

    git_commit = subprocess.run(
        [
            "git",
            "commit",
            "-m",
            f"chore: cursor snapshot at tickets HEAD {head_sha[:8]}",
        ],
        capture_output=True,
        text=True,
        cwd=str(repo_root),
        check=False,
        timeout=_GIT_TIMEOUT_S,
    )
    # "nothing to commit" is idempotent-success. Different git versions emit
    # the phrase on stdout vs stderr; check both before failing.
    _commit_output = git_commit.stdout + git_commit.stderr
    if git_commit.returncode != 0 and "nothing to commit" not in _commit_output:
        return False, False, branch, f"git commit failed: {git_commit.stderr.strip()}"

    return True, True, branch, f"cursor snapshot committed at tickets HEAD {head_sha[:8]}"


# ── Public entry point ──────────────────────────────────────────────────────


def run(repo_root: Path | None = None) -> StepResult:
    """Capture cursors from tickets branch and commit snapshot."""
    if repo_root is None:
        repo_root = Path(os.environ.get("REBAR_ROOT") or os.environ.get("PROJECT_ROOT") or Path(__file__).resolve().parents[4])  # project root

    snapshot_path = repo_root / "bridge_state" / "cursor-snapshot.json"

    try:
        # Step 1: Resolve tickets-branch HEAD SHA
        head_sha, err = _resolve_tickets_head(repo_root)
        if err:
            return StepResult(name="cursor_snapshot", ok=False, message=err)
        assert head_sha is not None  # narrowing for type checkers

        # Step 2: Idempotence check — skip the write+commit if the snapshot on
        # disk already pins the same tickets head.
        if snapshot_path.exists():
            # Treat any read failure (JSON shape, binary garbage, transient IO,
            # permissions glitch) as "corrupt — overwrite it" so the reconciler
            # self-heals from bad disk state rather than getting stuck in a
            # permanent-failure loop. The original `except Exception: pass`
            # was over-broad; this enumerates the realistic failure modes
            # without silencing genuine bugs.
            try:
                existing = json.loads(snapshot_path.read_text(encoding="utf-8"))
                if existing.get("head_sha") == head_sha:
                    return StepResult(
                        name="cursor_snapshot",
                        ok=True,
                        message="snapshot already current (idempotent)",
                        details={"skipped": True, "head_sha": head_sha},
                    )
            except (json.JSONDecodeError, OSError, UnicodeDecodeError):
                pass  # corrupt snapshot — overwrite it

        # Step 3+4: Load outbound checkpoint and inbound cursor
        outbound, inbound = _load_cursors(repo_root)

        # Step 5: Build snapshot dict
        snapshot = {
            "head_sha": head_sha,
            "outbound_checkpoint": outbound,
            "inbound_cursor": inbound,
        }

        # Step 6: Atomic write
        _write_snapshot_atomically(snapshot_path, snapshot)

        # Step 7: Commit (no-op when not on tickets branch — see helper docstring).
        # The helper returns the branch it observed, so we don't re-spawn another
        # `git rev-parse --abbrev-ref HEAD` subprocess to populate details (and
        # avoid a TOCTOU window between decision and details capture).
        ok, committed, branch, message = _commit_to_tickets_branch(
            repo_root, snapshot_path, head_sha
        )
        return StepResult(
            name="cursor_snapshot",
            ok=ok,
            message=message,
            details={
                "head_sha": head_sha,
                "outbound": outbound,
                "inbound": inbound,
                "branch": branch,
                # Machine-checkable flag so callers can distinguish "commit
                # happened" from "ok=True but commit was skipped because the
                # current branch was not 'tickets'". Downstream automation
                # that requires a durable tickets-branch commit must check
                # `details['committed']`, not `result.ok`.
                "committed": committed,
            },
        )

    except subprocess.TimeoutExpired as exc:
        # Distinct envelope for timeouts so operators can tell 'git hung past
        # 30s' apart from any other crash class. Only `git commit` / `git add`
        # actually hold .git/index.lock — so the hint is conditional rather
        # than unconditional (avoids steering investigators toward a lock that
        # was never acquired when e.g. `git rev-parse` timed out).
        _cmd_parts = exc.cmd if isinstance(exc.cmd, (list, tuple)) else [str(exc.cmd)]
        _cmd = " ".join(str(a) for a in _cmd_parts)
        _holds_lock = (
            len(_cmd_parts) >= 2 and _cmd_parts[0] == "git" and _cmd_parts[1] in ("add", "commit")
        )
        _lock_hint = (
            " — .git/index.lock may be held; investigate slow pre-commit hooks "
            "or repo lock contention"
            if _holds_lock
            else ""
        )
        return StepResult(
            name="cursor_snapshot",
            ok=False,
            message=f"git command timed out after {exc.timeout}s: {_cmd}{_lock_hint}",
        )
    except Exception as exc:
        return StepResult(
            name="cursor_snapshot",
            ok=False,
            message=f"unexpected error: {exc}",
        )
