"""Publish a reclaimed ``tickets`` baseline, or roll it back (ADR 0106 §6, story S2).

Both adapters enforce one precondition: replace the published history only if the remote
is unchanged since T0, the state the reclaim was computed from; otherwise abort with no
write so the operator recomputes and retries.

* **git ref remotes** (bare, GitHub, Gerrit): one ``git push --atomic`` that creates the
  backup ref ``refs/tickets-backup/pre-reclaim-<stamp>`` at T0 and moves the branch with
  ``--force-with-lease=<branch>:T0``. Atomicity means a moved remote leaves both refs
  untouched.
* **S3 (git-remote-s3)** has no ref CAS, and its auto-doctor folds divergent heads back in
  (:mod:`rebar._store.s3_doctor`), which would resurrect the rewritten-away history. The
  adapter therefore replaces the bundle set exclusively under the helper's per-ref lock:
  assert the bundle heads equal T0, copy every bundle to a rollback prefix, write one
  bundle for the new tip, then delete the originals. No doctor runs.

:func:`stamp_superseding_epoch` commits the superseding store-epoch record onto a tip
before it is published, so clones can prove the new history replaced theirs
(:func:`rebar._store.compat.remote_epoch_supersedes_local`) and take the stale-clone
override in :mod:`rebar._store.stale_override`. A rollback publishes the restored history
with its own superseding epoch, so clones that already moved onto the reclaimed baseline
are overridden back the same way.

Nothing here runs automatically; S3's dry-run harness and a human go/no-go drive it.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from rebar._commands.reclaim_collapse import _assert_shadow_tracker
from rebar._store import compat
from rebar._store.gitutil import run_git

#: Remote refs that keep the pre-swap history; a rollback publishes one of them again.
BACKUP_REF_PREFIX = "refs/tickets-backup/pre-reclaim-"

_GIT_TIMEOUT_S = 600.0


class PublishSwapError(RuntimeError):
    """A publish step failed; the message says whether anything was written."""


class PublishAborted(PublishSwapError):
    """The remote moved since T0. Nothing was written; recompute the reclaim and retry."""


@dataclass(frozen=True)
class GitRefSwap:
    remote_url: str
    branch: str
    old_tip: str
    new_tip: str
    backup_ref: str


@dataclass(frozen=True)
class BundleSwap:
    ref: str
    old_heads: tuple[str, ...]
    new_tip: str
    new_key: str
    rollback_prefix: str
    backed_up_keys: tuple[str, ...]
    deleted_keys: tuple[str, ...]


# raw-git-ok: commits only on the safety-checked disposable shadow clone
def stamp_superseding_epoch(
    shadow: str | os.PathLike[str], new_epoch: str, *, replaced_tip: str = "HEAD"
) -> str:
    """Commit, on the shadow's HEAD, a store-compat record superseding ``replaced_tip``'s.

    The record is ``replaced_tip``'s record re-stamped by
    :func:`compat.superseding_record`, so its lineage names every epoch the published
    history has carried. Forward swap: stamp the collapsed tip (``replaced_tip`` = HEAD).
    Rollback: check out the backup tip, then stamp with ``replaced_tip`` = the rewritten
    tip, so clones on either history can prove the restored one supersedes theirs.
    Returns the new HEAD.
    """
    root = Path(shadow).resolve()
    _assert_shadow_tracker(root)
    record = _record_at(root, replaced_tip)
    updated = compat.superseding_record(record, new_epoch)
    path = root / compat.COMPAT_FILENAME
    path.write_text(json.dumps(updated, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    _git(root, "add", "--", compat.COMPAT_FILENAME)
    _git(
        root,
        "commit",
        "--quiet",
        "--no-verify",
        "-m",
        f"rebar reclaim: supersede store epoch with {new_epoch}",
    )
    return _git(root, "rev-parse", "HEAD").strip()


def _record_at(root: Path, rev: str) -> dict[str, object]:
    shown = run_git(root, "cat-file", "blob", f"{rev}:{compat.COMPAT_FILENAME}", check=False)
    if shown.returncode != 0:
        # No record yet: the implicit legacy store, which has no epoch.
        return {"format_version": compat.CURRENT_FORMAT_VERSION, "required_capabilities": []}
    try:
        record = json.loads(shown.stdout)
    except json.JSONDecodeError as err:
        raise PublishSwapError(f"{rev}:{compat.COMPAT_FILENAME} is not JSON: {err}") from err
    if not isinstance(record, dict):
        raise PublishSwapError(f"{rev}:{compat.COMPAT_FILENAME} is not a JSON object")
    return record


def publish_git_ref(
    shadow: str | os.PathLike[str],
    remote_url: str,
    *,
    branch: str,
    expected_old_tip: str,
    new_tip: str,
    now: datetime | None = None,
) -> GitRefSwap:
    """Replace ``branch`` on a git remote with ``new_tip`` iff it still points at T0.

    One atomic push creates the backup ref at T0 and force-moves the branch under a
    ``--force-with-lease`` on T0. If the branch moved, git rejects the lease and the
    atomic push writes neither ref: :class:`PublishAborted`.
    """
    root = Path(shadow).resolve()
    _assert_shadow_tracker(root)
    backup_ref = BACKUP_REF_PREFIX + _stamp(now)
    proc = _push_atomic(
        root,
        remote_url,
        lease=(f"refs/heads/{branch}", expected_old_tip),
        refspecs=(f"{new_tip}:refs/heads/{branch}", f"{expected_old_tip}:{backup_ref}"),
    )
    if proc.returncode != 0:
        _raise_push_failure(root, remote_url, branch, expected_old_tip, proc)
    return GitRefSwap(remote_url, branch, expected_old_tip, new_tip, backup_ref)


def rollback_git_ref(
    shadow: str | os.PathLike[str],
    remote_url: str,
    *,
    branch: str,
    expected_current_tip: str,
    restored_tip: str,
) -> None:
    """Publish ``restored_tip`` (a backup tip re-stamped with a superseding epoch).

    Guarded by a lease on the rewritten tip the rollback replaces, exactly like the
    forward swap. Clones already on the rewritten baseline then see a superseding epoch
    and take the stale-clone override back onto the restored history.
    """
    root = Path(shadow).resolve()
    _assert_shadow_tracker(root)
    proc = _push_atomic(
        root,
        remote_url,
        lease=(f"refs/heads/{branch}", expected_current_tip),
        refspecs=(f"{restored_tip}:refs/heads/{branch}",),
    )
    if proc.returncode != 0:
        _raise_push_failure(root, remote_url, branch, expected_current_tip, proc)


def publish_s3_bundle_set(
    shadow: str | os.PathLike[str],
    s3: Any,
    *,
    ref: str,
    expected_heads: set[str],
    new_tip: str,
    now: datetime | None = None,
) -> BundleSwap:
    """Replace a git-remote-s3 ref's bundle set with one bundle of ``new_tip``.

    Holds the helper's per-ref lock throughout. The bundle heads must equal T0's
    (``expected_heads``) or the swap aborts before any write. Every original bundle is
    copied under ``<prefix>/<ref>/PROTECTED#rollback-<stamp>/`` before the new bundle is
    written and the originals deleted. The auto-doctor is never invoked, so the replaced
    history is not merged back.
    """
    root = Path(shadow).resolve()
    _assert_shadow_tracker(root)
    try:
        lock_key = s3.acquire_lock(ref)
    except Exception as err:
        raise PublishAborted(f"bundle lock for {ref} is held; nothing written") from err
    if lock_key is None:
        raise PublishAborted(f"bundle lock for {ref} is held; nothing written")
    try:
        return _swap_bundles_locked(root, s3, ref, expected_heads, new_tip, _stamp(now))
    finally:
        s3.release_lock(ref, lock_key)


def _swap_bundles_locked(
    root: Path, s3: Any, ref: str, expected: set[str], new_tip: str, stamp: str
) -> BundleSwap:
    keys = [b["Key"] for b in s3.get_bundles_for_ref(ref)]
    heads = {_sha_from_key(k) for k in keys}
    if heads != expected:
        raise PublishAborted(
            f"bundle heads for {ref} are {sorted(heads)}, expected T0 {sorted(expected)}; "
            "nothing written"
        )
    rollback_prefix = f"{s3.prefix}/{ref}/PROTECTED#rollback-{stamp}"
    backed_up = tuple(
        _copy_object(s3, key, f"{rollback_prefix}/{os.path.basename(key)}") for key in keys
    )
    new_key = f"{s3.prefix}/{ref}/{new_tip}.bundle"
    _put_bundle(root, s3, ref, new_tip, new_key)
    deleted = tuple(key for key in keys if key != new_key)
    for key in deleted:
        s3.s3.delete_object(Bucket=s3.bucket, Key=key)
    s3.init_remote_head(ref)
    return BundleSwap(
        ref, tuple(sorted(heads)), new_tip, new_key, rollback_prefix, backed_up, deleted
    )


def _copy_object(s3: Any, src: str, dst: str) -> str:
    with tempfile.TemporaryDirectory() as tmp:
        local = os.path.join(tmp, "bundle")
        s3.s3.download_file(Bucket=s3.bucket, Key=src, Filename=local)
        with open(local, "rb") as fh:
            s3.s3.put_object(Bucket=s3.bucket, Key=dst, Body=fh.read())
    return dst


def _put_bundle(root: Path, s3: Any, ref: str, tip: str, key: str) -> None:
    """Upload one bundle carrying ``HEAD`` and ``refs/heads/<ref>`` at ``tip``.

    The bundle is cut from a scratch bare repository so the shadow's own refs never move.
    """
    with tempfile.TemporaryDirectory() as tmp:
        scratch = os.path.join(tmp, "bundle-src.git")
        _git(root, "init", "--quiet", "--bare", scratch)
        _git(root, "push", "--quiet", scratch, f"{tip}:refs/heads/{ref}")
        _git(Path(scratch), "symbolic-ref", "HEAD", f"refs/heads/{ref}")
        bundle = os.path.join(tmp, "swap.bundle")
        _git(Path(scratch), "bundle", "create", bundle, "HEAD", f"refs/heads/{ref}")
        with open(bundle, "rb") as fh:
            s3.s3.put_object(Bucket=s3.bucket, Key=key, Body=fh.read())


def _raise_push_failure(
    root: Path, url: str, branch: str, expected: str, proc: subprocess.CompletedProcess[str]
) -> None:
    """Classify a failed atomic push. It wrote nothing either way; say why."""
    detail = (proc.stderr or proc.stdout or "").strip()
    listed = run_git(
        root, "ls-remote", url, f"refs/heads/{branch}", check=False, timeout=_GIT_TIMEOUT_S
    )
    current = listed.stdout.split()[0] if listed.returncode == 0 and listed.stdout else None
    if current is not None and current != expected:
        raise PublishAborted(
            f"remote {branch} is at {current}, not T0 {expected}; nothing written: {detail}"
        )
    raise PublishSwapError(f"atomic push failed; nothing written: {detail}")


# raw-git-ok: pushes from the shadow clone to an explicit throwaway or operator-chosen URL
def _push_atomic(
    root: Path, url: str, *, lease: tuple[str, str], refspecs: tuple[str, ...]
) -> subprocess.CompletedProcess[str]:
    ref, old = lease
    return run_git(
        root,
        "push",
        "--atomic",
        "--porcelain",
        f"--force-with-lease={ref}:{old}",
        url,
        *refspecs,
        check=False,
        timeout=_GIT_TIMEOUT_S,
    )


# raw-git-ok: runs only inside the safety-checked shadow clone or a scratch bundle repo
def _git(root: Path, *args: str) -> str:
    proc = run_git(root, *args, check=False, timeout=_GIT_TIMEOUT_S)
    if proc.returncode != 0:
        raise PublishSwapError(f"git {' '.join(args[:2])} failed: {proc.stderr}")
    return proc.stdout


def _sha_from_key(key: str) -> str:
    return os.path.basename(key).removesuffix(".bundle")


def _stamp(now: datetime | None) -> str:
    moment = now if now is not None else datetime.now(UTC)
    return moment.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
