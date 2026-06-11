"""Shared attestation helpers for rebar_reconciler band modules.

Provides verify_attested_commit() which checks that a commit is GPG-signed
and that its committer is a human (not a bot in the allowlist), and
verify_manifest_hash() which checks an attested manifest hash against the
on-disk manifest contents.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
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
    this fail-fast behaviour over a cryptic ``AcliClient()`` TypeError
    (the no-arg constructor was the root cause of the bands' production
    crash before this helper existed).

    Returns:
        A configured ``AcliClient`` instance loaded from the sibling
        ``acli-integration.py`` (under the rebar engine scripts dir) via importlib.

    Raises:
        RuntimeError: If any required JIRA_* env var is missing or empty.
    """
    required = ("JIRA_URL", "JIRA_USER", "JIRA_API_TOKEN")
    missing = [name for name in required if not os.environ.get(name)]
    if missing:
        raise RuntimeError(
            f"missing JIRA_* environment variables: {', '.join(missing)} "
            "(required to construct AcliClient for bootstrap band execution)"
        )

    import importlib.util  # local import — keeps top-level deps minimal

    acli_path = Path(__file__).parent.parent / "acli-integration.py"
    spec = importlib.util.spec_from_file_location("acli_integration", acli_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"acli-integration.py not found at {acli_path}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("acli_integration", mod)
    spec.loader.exec_module(mod)

    return mod.AcliClient(
        jira_url=os.environ["JIRA_URL"],
        user=os.environ["JIRA_USER"],
        api_token=os.environ["JIRA_API_TOKEN"],
        jira_project=os.environ.get("JIRA_PROJECT", "DIG"),
    )


def verify_attested_commit(sha: str, allowlist: list[str]) -> bool:
    """Return True when *sha* is human-attested.

    TODO(bug TBD — F14): the subprocess calls inherit the caller's CWD;
    when a band is invoked outside the repo containing the attestation
    commit (e.g., from a sibling worktree), ``git verify-commit`` looks
    up the SHA in the wrong repo and fails-closed. Either accept a
    ``repo_root`` argument and pass ``-C <repo_root>`` to git, or
    document the CWD requirement at every caller. Deferred — non-crash
    edge case, but operator-visible when bands run from anywhere other
    than the host repo root.

    A commit is considered human-attested when ALL of the following hold:

    1. ``git verify-commit <sha>`` exits 0 (valid GPG signature).
    2. The committer email is NOT in *allowlist* (bot emails are excluded
       because automated commits do not count as human review).

    Returns False on any subprocess error so callers can treat failures as
    "not attested" without crashing.

    Args:
        sha: Full or abbreviated commit SHA to verify.
        allowlist: List of bot committer email addresses to exclude.

    Returns:
        True if the commit passes both checks, False otherwise.
    """
    try:
        result = subprocess.run(
            ["git", "verify-commit", sha],
            capture_output=True,
            check=False,
        )
        if result.returncode != 0:
            return False
    except Exception:  # noqa: BLE001
        return False

    try:
        email_result = subprocess.run(
            ["git", "log", "-1", "--format=%ae", sha],
            capture_output=True,
            text=True,
            check=False,
        )
        if email_result.returncode != 0:
            return False
        committer_email = email_result.stdout.strip()
    except Exception:  # noqa: BLE001
        return False

    if committer_email in allowlist:
        return False

    return True
