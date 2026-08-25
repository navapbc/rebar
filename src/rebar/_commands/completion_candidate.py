"""Isolated Git candidate for receipt-aware completion publication.

The shared tickets checkout must never carry a certified close that remote receipt
validation later rejects.  A candidate therefore lives in a cheap local clone which
shares the source object database, checks out only the ticket being closed, and owns its
own index and HEAD.  Generic tracker pushes can only see the shared checkout's HEAD, so a
failed or interrupted candidate cannot leak into a later write.
"""

from __future__ import annotations

import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

from rebar._snapshot.ticket_view import TicketsOID
from rebar._store.gitutil import run_git_bounded

_GIT_TIMEOUT_SECONDS = 30


class CandidateError(RuntimeError):
    """An isolated candidate repository could not be prepared."""


def _git(cwd: str | None, *args: str):
    return run_git_bounded(cwd, *args, timeout=_GIT_TIMEOUT_SECONDS)


def _require(proc, operation: str) -> None:
    if proc.returncode == 0:
        return
    detail = (proc.stderr or proc.stdout or "unknown git failure").strip()
    raise CandidateError(f"{operation} failed: {detail}")


@dataclass(frozen=True)
class CompletionCandidate:
    """One private close commit and the disposable repository that owns it."""

    root: str
    tracker: str
    base_oid: TicketsOID
    commit_oid: TicketsOID | None = None

    def with_commit(self, commit_oid: TicketsOID) -> CompletionCandidate:
        if not isinstance(commit_oid, TicketsOID):
            raise TypeError("candidate commit must be a TicketsOID")
        return CompletionCandidate(
            root=self.root,
            tracker=self.tracker,
            base_oid=self.base_oid,
            commit_oid=commit_oid,
        )

    def cleanup(self) -> None:
        """Remove only this operation's mkdtemp-owned repository."""
        shutil.rmtree(self.root, ignore_errors=True)


def _copy_commit_identity(source: str, candidate: str) -> None:
    """Preserve tracker-local author identity without copying unrelated config."""
    for key in ("user.name", "user.email"):
        value = _git(source, "config", "--get", key)
        if value.returncode != 0 or not value.stdout.strip():
            continue
        configured = _git(candidate, "config", key, value.stdout.strip())
        _require(configured, f"configure candidate {key}")
    # A ticket-store commit is an internal data-store transaction.  It must not inherit an
    # ambient interactive signing policy which could hang while the store lock is held.
    configured = _git(candidate, "config", "commit.gpgsign", "false")
    _require(configured, "disable candidate Git commit signing")


def prepare_candidate(
    tracker: str,
    base_oid: TicketsOID,
    ticket_id: str,
    *,
    run_id: str,
) -> CompletionCandidate:
    """Create an object-sharing, sparse checkout at ``base_oid``.

    Clone setup and sparse materialization happen before the shared ticket-store lock is
    requested.  ``--shared`` installs an object alternate instead of copying the store;
    sparse checkout materializes only root metadata plus the demanded ticket directory.
    The random ``mkdtemp`` root and run-id prefix keep parallel verifier candidates apart.
    """
    if not isinstance(base_oid, TicketsOID):
        raise TypeError("candidate base must be a TicketsOID")
    safe_run = "".join(ch for ch in str(run_id) if ch.isalnum() or ch in "-_")[:24]
    root = tempfile.mkdtemp(prefix=f"rebar-close-{safe_run or 'run'}-")
    candidate_path = str(Path(root) / "tickets")
    candidate = CompletionCandidate(root, candidate_path, base_oid)
    try:
        cloned = _git(
            None,
            "clone",
            "--shared",
            "--no-checkout",
            "--quiet",
            tracker,
            candidate_path,
        )
        _require(cloned, "prepare isolated completion candidate")
        sparse = _git(candidate_path, "sparse-checkout", "init", "--cone")
        _require(sparse, "initialize candidate sparse checkout")
        selected = _git(
            candidate_path,
            "sparse-checkout",
            "set",
            "--skip-checks",
            ticket_id,
        )
        _require(selected, "select candidate ticket directory")
        checked_out = _git(  # raw-git-ok: checkout mutates only this disposable candidate
            candidate_path, "checkout", "--detach", base_oid.value
        )
        _require(checked_out, "pin candidate tracker revision")
        _copy_commit_identity(tracker, candidate_path)
        return candidate
    except BaseException:
        candidate.cleanup()
        raise


__all__ = [
    "CandidateError",
    "CompletionCandidate",
    "prepare_candidate",
]
