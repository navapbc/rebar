"""Provide remote compare-and-swap for reconciler ref leases.

A force-with-lease stale rejection becomes the exit-128 ``update-ref`` shape
understood by the shared CAS classifier. Non-lease and ambiguous push failures
retain their original error, preventing false ``LeaseLostError`` reports. Each
entry point resolves Git, timeout, and logging dependencies through the
caller-supplied ``core`` module. This preserves by-path module identity,
monkeypatch seams, logger ownership, and one-way dependency flow.
"""

from __future__ import annotations

import contextlib
import subprocess
import uuid
from collections.abc import Iterator
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


# ---------------------------------------------------------------------------
# Commit-chain writes (metaphoric-late-creature, ADR 0031 amendment). Every write is a
# commit whose parent is the tip the writer read, pushed as a plain fast-forward from a
# temporary named ref: git-remote-s3 bundles the push source, so it cannot push a bare
# oid, and it gives no compare on a forced push or a delete. GitHub and local remotes
# reject a non-fast-forward natively, so one mechanism serves every remote.
# ---------------------------------------------------------------------------

TMP_REF_PREFIX = "refs/reconciler/tmp/"
_IDENTITY = ("-c", "user.name=rebar-reconciler", "-c", "user.email=reconciler@rebar.invalid")
_UNKNOWN = object()


def wrap_commit(
    core: ModuleType, repo_root: Path, payload_oid: str, name: str, parent: str | None
) -> str:
    """Wrap *payload_oid* as ``<name>`` in a one-file tree and commit it onto *parent*."""
    entry = f"100644 blob {payload_oid}\t{name}\n".encode()
    tree = (
        core._git(  # raw-git-ok: write seam internal (object write, no ref moves)
            repo_root, ["mktree"], timeout=core._LOCAL_TIMEOUT_SECS, text=False, stdin=entry
        )
        .stdout.decode("utf-8")
        .strip()
    )
    args = [
        *_IDENTITY,
        "commit-tree",
        "--no-gpg-sign",
        tree,
        "-m",
        f"reconciler {name} {uuid.uuid4().hex}",
    ]
    if parent is not None:
        args[len(_IDENTITY) + 3 : len(_IDENTITY) + 3] = ["-p", parent]
    return core._git(  # raw-git-ok: write seam internal (object write, no ref moves)
        repo_root, args, timeout=core._LOCAL_TIMEOUT_SECS
    ).stdout.strip()


@contextlib.contextmanager
def temp_ref(core: ModuleType, repo_root: Path, oid: str) -> Iterator[str]:
    """Point a throwaway ``refs/reconciler/tmp/<id>`` at *oid* for the push source."""
    ref = f"{TMP_REF_PREFIX}{uuid.uuid4().hex}"
    core._git(  # raw-git-ok: write seam internal (private temp ref)
        repo_root, ["update-ref", ref, oid], timeout=core._LOCAL_TIMEOUT_SECS
    )
    try:
        yield ref
    finally:
        core._git(  # raw-git-ok: write seam internal (private temp ref)
            repo_root, ["update-ref", "-d", ref], timeout=core._LOCAL_TIMEOUT_SECS, check=False
        )


def remote_tip(core: ModuleType, repo_root: Path, ref: str, remote: str) -> object:
    """The remote's current oid for *ref*: an oid, ``None`` (absent), or ``_UNKNOWN``."""
    result = core._git(
        repo_root,
        ["ls-remote", "--exit-code", remote, ref],
        timeout=core._REMOTE_TIMEOUT_SECS,
        check=False,
    )
    if result.returncode == 2:
        return None
    if result.returncode != 0:
        return _UNKNOWN
    for line in (result.stdout or "").splitlines():
        oid, _, name = line.partition("\t")
        if name.strip() == ref:
            return oid.strip()
    return None


def push_fast_forward(
    core: ModuleType, repo_root: Path, ref: str, new_oid: str, old_oid: str, remote: str
) -> None:
    """Fast-forward *ref* on *remote* to *new_oid*; decide a rejection by re-reading the tip.

    Our commit at the tip is success (a reply lost after the update); a tip that moved off
    the observed *old_oid* is a CAS mismatch, raised in the exit-128 ``update-ref`` shape the
    shared classifier reads; an unchanged tip is a genuine failure carrying git's stderr.
    """
    with temp_ref(core, repo_root, new_oid) as source:
        result = core._git(
            repo_root,
            ["push", remote, f"{source}:{ref}"],
            timeout=core._REMOTE_TIMEOUT_SECS,
            check=False,
        )
    if result.returncode == 0:
        return
    tip = remote_tip(core, repo_root, ref, remote)
    if tip == new_oid:
        return
    observed = None if old_oid == "0" * 40 else old_oid
    if tip is not _UNKNOWN and tip != observed:
        core.logger.warning(
            "ref-lock: push to %s %s rejected and the tip moved (expected %s, now %s) — "
            "CAS mismatch: %s",
            remote,
            ref,
            old_oid,
            tip,
            (result.stderr or "").strip()[-500:],
        )
        raise subprocess.CalledProcessError(128, ["git", "update-ref", ref])
    raise core.RefLockPushError(remote, ref, result)
