"""Provide remote compare-and-swap for reconciler ref leases.

A force-with-lease stale rejection becomes the exit-128 ``update-ref`` shape
understood by the shared CAS classifier. Non-lease and ambiguous push failures
retain their original error, preventing false ``LeaseLostError`` reports. Each
entry point resolves Git, timeout, and logging dependencies through the
caller-supplied ``core`` module. This preserves by-path module identity,
monkeypatch seams, logger ownership, and one-way dependency flow.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from types import ModuleType

from rebar._store import git_outcome as _git_outcome

# Shared marker tables separate stale force-with-lease rejections from hook,
# server, and ref-lock failures. The aliases preserve names used by ``_ref_lock``
# and its tests.
PUSH_REJECT_MARKERS = _git_outcome.PUSH_REJECT_MARKERS
LEASE_MISMATCH_MARKER = _git_outcome.LEASE_MISMATCH_MARKER
NON_CAS_REJECT_MARKERS: tuple[str, ...] = _git_outcome.NON_CAS_REJECT_MARKERS


def is_cas_mismatch_stderr(stderr: str) -> bool:
    """Whether *stderr* (lowercased) shows the LEASE moved, not merely a rejection.

    "stale info" is conclusive; a broader marker counts only when nothing names a
    non-lease cause, so ambiguity fails closed per the documented posture. A lookup
    against the shared registry under the ``lease-push`` operation — the SAME text
    classifies differently under ``local`` and ``ref-cas``, which is why the registry is
    keyed by the pair and this verdict is not merged with theirs.
    """
    return _git_outcome.is_lease_mismatch(stderr)


def push_lease_cas(
    core: ModuleType,
    repo_root: Path,
    ref: str,
    old_oid: str,
    remote: str,
    refspec: str,
) -> None:
    """Do a ``--force-with-lease=<ref>:<old>`` push of *refspec* to *remote*.

    Shared by acquire (``<new-oid>:<ref>``) and release (``:<ref>`` delete). A
    rejected lease (remote ref moved) is re-raised in the update-ref exit-128
    shape so the shared ``_cas_once`` seam classifies it as a CAS mismatch; a
    genuine transport failure is logged and re-raised (fail-closed).

    *core* is the calling ``_ref_lock`` module; ``_git``, ``_REMOTE_TIMEOUT_SECS``
    and ``logger`` are read from it at call time (see the module docstring).
    """
    logger = core.logger
    result = core._git(
        repo_root,
        ["push", f"--force-with-lease={ref}:{old_oid}", remote, refspec],
        timeout=core._REMOTE_TIMEOUT_SECS,
        check=False,
    )
    if result.returncode == 0:
        return
    stderr = (result.stderr or "").lower()
    if is_cas_mismatch_stderr(stderr):
        # Log the rejected lease before reporting that another holder won the CAS.
        logger.warning(
            "ref-lock: push to %s %s (expected oid %s) classified as CAS mismatch "
            "(lease moved) — stderr: %s",
            remote,
            ref,
            old_oid,
            (result.stderr or "").strip()[:200],
        )
        raise subprocess.CalledProcessError(128, ["git", "update-ref", ref])
    logger.warning(
        "ref-lock: git push to %s %s failed (exit %s) — fail-closed: %s",
        remote,
        ref,
        result.returncode,
        (result.stderr or "").strip()[:200],
    )
    raise subprocess.CalledProcessError(
        result.returncode, result.args, result.stdout, result.stderr
    )
