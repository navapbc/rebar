"""Shared attestation helpers for rebar_reconciler band modules.

Provides verify_attested_commit() which checks that a commit is GPG-signed
and that its committer is a human (not a bot in the allowlist), and
verify_manifest_hash() which checks an attested manifest hash against the
on-disk manifest contents.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any


def verify_manifest_hash(manifest_path: Path, attested_hash: str) -> bool:
    """Return True when *manifest_path*'s SHA-256 equals *attested_hash*.

    Used by every band's cmd_gate to detect manifest tampering between
    plan-time attestation and apply-time execution. Returns False on
    missing manifest, empty attested hash, or read error rather than
    raising — gate callers should treat any failure as "not attested".

    Args:
        manifest_path: Path to the manifest file under attestation.
        attested_hash: Expected hex-encoded SHA-256 from the attestation
            record (``attested.json["manifest_hash"]``).

    Returns:
        True iff the manifest exists and its SHA-256 matches.
    """
    if not attested_hash:
        return False
    try:
        actual = hashlib.sha256(manifest_path.read_bytes()).hexdigest()
    except OSError:
        return False
    return actual == attested_hash


def build_acli_client_from_env() -> Any:
    """Construct an AcliClient using JIRA_* environment variables.

    Reads ``JIRA_URL``, ``JIRA_USER``, ``JIRA_API_TOKEN``, and optional
    ``JIRA_PROJECT`` (defaulting to ``"DIG"``). Raises RuntimeError with
    a clear message when any required variable is missing — bands prefer
    this fail-fast behaviour over a cryptic no-arg-constructor TypeError
    (the no-arg constructor was the root cause of the bands' production
    crash before this helper existed).

    Ticket 97f2/bbf1: the transport is obtained from the configured backend
    (``select_backend(load_config()).transport``) and the fail-fast missing-env
    guard is delegated to the backend's neutral ``assert_env_ready()`` — so this
    core module imports NO ``adapters.jira`` symbol while preserving the exact
    RuntimeError contract (same message naming the missing JIRA_* var(s)).

    Returns:
        The configured backend's transport (a ``TicketTransport``, i.e. an
        AcliClient).

    Raises:
        RuntimeError: If any required JIRA_* env var is missing or empty.
    """
    from rebar.config import load_config
    from rebar_reconciler._backend_registry import select_backend

    backend = select_backend(load_config())
    backend.assert_env_ready()
    return backend.transport


def verify_attested_commit(sha: str, allowlist: list[str], repo_root: Path | None = None) -> bool:
    """Return True when *sha* is human-attested.

    F14 fix: pass ``repo_root`` to run the git subprocesses against a SPECIFIC
    repository via ``git -C <repo_root>`` instead of inheriting the caller's CWD.
    Without it, a band invoked outside the repo containing the attestation commit
    (e.g. from a sibling worktree) looked the SHA up in the wrong repo and
    fail-closed spuriously. ``repo_root=None`` (the default) preserves the prior
    CWD-relative behavior for existing callers.

    A commit is considered human-attested when ALL of the following hold:

    1. ``git verify-commit <sha>`` exits 0 (valid GPG signature).
    2. The committer email is NOT in *allowlist* (bot emails are excluded
       because automated commits do not count as human review).

    Returns False on any subprocess error so callers can treat failures as
    "not attested" without crashing.

    Args:
        sha: Full or abbreviated commit SHA to verify.
        allowlist: List of bot committer email addresses to exclude.
        repo_root: Repository to run git in (``git -C <repo_root>``); ``None``
            (default) runs git in the caller's CWD, preserving prior behavior.

    Returns:
        True if the commit passes both checks, False otherwise.
    """
    # git_adapter is the reconciler's single git seam; it forwards repo_root as
    # ``git -C <repo_root>`` and, when repo_root is None, omits ``-C`` entirely —
    # preserving the F14 fix (run in the RIGHT repo) AND the default CWD-relative
    # behaviour byte-for-byte.
    from rebar_reconciler import git_adapter

    try:
        # Bound the GPG verify: it can hang on a gpg-agent / keyserver lookup.
        # TimeoutExpired is caught below and treated as a verification failure.
        result = git_adapter.verify_commit(repo_root, sha, timeout=15)
        if result.returncode != 0:
            return False
    except Exception:  # noqa: BLE001 — any verify failure (incl. timeout) treated as unverified
        return False

    try:
        email_result = git_adapter.commit_email(repo_root, sha, timeout=10)
        if email_result.returncode != 0:
            return False
        committer_email = email_result.stdout.strip()
    except Exception:  # noqa: BLE001 — any lookup failure treated as unverified (fail-closed)
        return False

    if committer_email in allowlist:
        return False

    return True
