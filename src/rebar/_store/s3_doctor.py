"""Losslessly heal multiple S3 bundles advertising one ticket-store ref.

Fold all heads into the published ref under the write lock, upload its merged bundle
before deleting originals, and leave the checked-out branch out of unrelated merges.
The optional S3 helper loads only inside guarded functions.
"""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
from collections.abc import Callable
from typing import Any

from rebar._optional import require_s3_helper
from rebar._store import lock as _lock
from rebar._store.gitutil import is_transient_object_read_error, run_git_write

logger = logging.getLogger(__name__)

_GIT_TIMEOUT = 60
_WRITE_LOCK_TIMEOUT = 30


class S3DoctorConflict(Exception):
    """Raised when the lossless merge hits an unresolved conflict in a non-derived file.

    Carries a ``.hint`` mentioning ``fsck-recover`` so the operator has the exact next step.
    """

    def __init__(self, message: str, *, hint: str | None = None) -> None:
        super().__init__(message)
        self.hint = hint or "tickets store heads conflict; run: rebar store fsck-recover"


# raw-git-ok: locked store seam internal
def _git(base: str, *args: str) -> subprocess.CompletedProcess:
    """Run idempotent doctor git operations through the bounded write seam.

    Its shared lock and retries cover transient object reads and index/ref locks;
    the doctor retains its own timeout.
    """
    return run_git_write(base, *args, check=False, timeout=_GIT_TIMEOUT)


def _default_factory(remote_url: str) -> Any:
    """Build the REAL git_remote_s3 helper for ``remote_url`` (lazy, guarded)."""
    require_s3_helper()
    from git_remote_s3 import S3Remote, parse_git_url

    scheme, profile, bucket, prefix = parse_git_url(remote_url)
    return S3Remote(scheme, profile, bucket, prefix)


def _sha_from_key(key: str) -> str:
    """Extract ``<sha>`` from a bundle key ``<prefix>/<ref>/<sha>.bundle``."""
    return os.path.basename(key)[: -len(".bundle")]


def heal_multi_bundle(
    base_path: str,
    remote: str,
    ref: str,
    *,
    s3remote_factory: Callable[[str], Any] | None = None,
) -> dict:
    """Merge a ref's bundles in the tracker and publish one lossless bundle.

    ``remote`` names the configured Git remote; ``s3remote_factory`` injects its
    S3 client from the resolved URL.
    """
    if s3remote_factory is None:
        s3remote_factory = _default_factory

    url_res = _git(base_path, "remote", "get-url", remote)
    if url_res.returncode != 0:
        return {
            "healed": False,
            "reason": "remote-not-found",
            "ref": ref,
            "merged_sha": None,
            "deleted_keys": [],
        }
    remote_url = url_res.stdout.strip()
    s3 = s3remote_factory(remote_url)

    # 2. Acquire the helper's per-ref lock — a raised exception ALSO means "locked".
    try:
        lock_key = s3.acquire_lock(ref)
    except Exception:  # noqa: BLE001 - any raised error from acquire_lock means "locked"
        lock_key = None
    if lock_key is None:
        return {
            "healed": False,
            "reason": "locked",
            "ref": ref,
            "merged_sha": None,
            "deleted_keys": [],
        }

    try:
        return _heal_locked(base_path, ref, s3)
    finally:
        s3.release_lock(ref, lock_key)


def _heal_locked(base_path: str, ref: str, s3: Any) -> dict:
    # 3. Re-enumerate under the lock (TOCTOU guard).
    bundles = s3.get_bundles_for_ref(ref)
    if len(bundles) <= 1:
        return {
            "healed": False,
            "reason": "noop",
            "ref": ref,
            "merged_sha": None,
            "deleted_keys": [],
        }

    _require_clean_worktree_on_ref(base_path, ref)

    original_keys = [b["Key"] for b in bundles]
    scratch_refs: list[str] = []
    try:
        head_refs = _fetch_heads(base_path, ref, s3, original_keys, scratch_refs)
        merged_sha = _fold_heads(base_path, ref, head_refs)
        deleted_keys = _publish_and_prune(base_path, ref, s3, merged_sha, original_keys)
    finally:
        for sref in scratch_refs:
            # Deletes the doctor's own disposable refs/rebar-doctor/* scratch refs (created
            # here to hold fetched bundle heads), not a tracker store write.
            _git(base_path, "update-ref", "-d", sref)  # raw-git-ok: doctor scratch ref cleanup

    s3.init_remote_head(ref)

    logger.info(
        "s3 doctor healed multi-bundle ref %s: merged heads %s -> %s; deleted %s",
        ref,
        [_sha_from_key(k) for k in original_keys],
        merged_sha,
        deleted_keys,
    )
    return {
        "healed": True,
        "ref": ref,
        "merged_sha": merged_sha,
        "deleted_keys": deleted_keys,
    }


def _on_published_ref(base_path: str, ref: str) -> bool:
    """Check whether *ref* is checked out and must move with its index and worktree."""
    return _git(base_path, "symbolic-ref", "--quiet", "--short", "HEAD").stdout.strip() == ref


def _require_clean_worktree_on_ref(base_path: str, ref: str) -> None:
    """Refuse a dirty checked-out published ref before downloading bundles.

    Healing a side branch or detached HEAD is safe because folding targets the
    published ref. Advancing that ref's checked-out worktree would overwrite tracked
    changes, so report the conflict after the single-bundle no-op check.
    """
    if not _on_published_ref(base_path, ref):
        return
    # TRACKED changes only: ``reset --hard`` overwrites those, but never removes untracked
    # files — and the tracker legitimately carries untracked runtime debris such as the write
    # lock, which must not be mistaken for work at risk.
    dirty = _git(base_path, "status", "--porcelain", "--untracked-files=no").stdout.strip()
    if not dirty:
        return
    raise S3DoctorConflict(
        f"refusing to heal ref {ref}: the tracker at {base_path} has {ref} checked out with "
        f"uncommitted changes, which the heal would overwrite",
        hint=(
            f"the heal advances the {ref} worktree onto the merged tip; commit or discard the "
            f"local changes in {base_path} and retry"
        ),
    )


def _fetch_heads(
    base_path: str,
    ref: str,
    s3: Any,
    original_keys: list[str],
    scratch_refs: list[str],
) -> list[str]:
    """Download every bundle and fetch its head into a local ``refs/rebar-doctor/<sha>`` ref."""
    head_refs: list[str] = []
    for key in original_keys:
        sha = _sha_from_key(key)
        with tempfile.NamedTemporaryFile(suffix=".bundle", dir=base_path, delete=False) as tmp:
            tmp_name = tmp.name
        try:
            s3.s3.download_file(Bucket=s3.bucket, Key=key, Filename=tmp_name)
            scratch_ref = f"refs/rebar-doctor/{sha}"
            # Explicit, fully-qualified refspec into a private scratch ref — never rely on
            # opportunistic FETCH_HEAD / remote-tracking writes (bugs 5546, 35f7).
            refspec = f"refs/heads/{ref}:{scratch_ref}"
            fetch = _git(base_path, "fetch", tmp_name, refspec)
            if fetch.returncode != 0:
                raise S3DoctorConflict(
                    f"could not fetch head {sha} from bundle for ref {ref}: {fetch.stderr}"
                )
            scratch_refs.append(scratch_ref)
            head_refs.append(scratch_ref)
        finally:
            try:
                os.unlink(tmp_name)
            except OSError:
                pass
    return head_refs


def _fold_heads(base_path: str, ref: str, head_refs: list[str]) -> str:
    """Fold heads into the published ref with worktree-free two-parent merges.

    Start at that ref, never the checked-out HEAD. ``merge-tree`` and ``commit-tree``
    honor the ticket merge policy without touching the index. Reachable heads leave
    the tip unchanged; conflicts raise ``S3DoctorConflict`` before publication.
    """
    branch = f"refs/heads/{ref}"
    with _lock.write_lock(base_path, timeout=_WRITE_LOCK_TIMEOUT, attempts=1, dual_window=True):
        probe = _git(base_path, "rev-parse", "--verify", "--quiet", f"{branch}^{{commit}}")
        # No local ref yet (a tracker whose branch is unborn): the first head BECOMES the tip,
        # exactly as the old fold's `git merge` fast-forwarded an unborn HEAD.
        tip = probe.stdout.strip() if probe.returncode == 0 else ""
        for head_ref in head_refs:
            if not tip:
                tip = _git(base_path, "rev-parse", "--verify", head_ref).stdout.strip()
                continue
            # Already reachable from the ref's tip → nothing to merge for this head.
            if _git(base_path, "merge-base", "--is-ancestor", head_ref, tip).returncode == 0:
                continue
            extra: list[str] = []
            if _git(base_path, "merge-base", tip, head_ref).stdout.strip() == "":
                extra = ["--allow-unrelated-histories"]
            merged = _git(base_path, "merge-tree", "--write-tree", *extra, tip, head_ref)
            if merged.returncode != 0:
                raise S3DoctorConflict(
                    f"tickets store heads conflict merging {head_ref} for ref {ref}: "
                    f"{merged.stderr or merged.stdout}"
                )
            tree = merged.stdout.strip().splitlines()[0].strip()
            commit = _git(
                base_path,
                "commit-tree",
                tree,
                "-p",
                tip,
                "-p",
                head_ref,
                "-m",
                f"Merge {head_ref} (s3 doctor auto-heal of ref {ref})",
            )
            if commit.returncode != 0:
                raise S3DoctorConflict(
                    f"could not build the merge commit folding {head_ref} into {ref}: "
                    f"{commit.stderr}"
                )
            tip = commit.stdout.strip()
        _publish_ref(base_path, ref, tip)
        return tip


# raw-git-ok: locked store seam internal (the doctor's ref-publish step, under the fold's lock)
def _publish_ref(base_path: str, ref: str, merged_sha: str) -> None:
    """Publish the merged ref while preserving the checked-out worktree state.

    Update a branch-only ref directly. For a clean checked-out ref, reset branch,
    index, and worktree together; the earlier cleanliness guard prevents data loss.
    """
    branch = f"refs/heads/{ref}"
    if _on_published_ref(base_path, ref):
        # Advances the ref's own checked-out worktree: branch, index and worktree together.
        moved = _git(base_path, "reset", "--hard", merged_sha, "--quiet")
    else:
        # Publishes the folded tip onto the ticket ref, leaving the worktree untouched.
        moved = _git(base_path, "update-ref", branch, merged_sha)
    if moved.returncode != 0:
        raise S3DoctorConflict(
            f"could not publish the folded tip {merged_sha} onto {branch}: {moved.stderr}"
        )


def _publish_and_prune(
    base_path: str,
    ref: str,
    s3: Any,
    merged_sha: str,
    original_keys: list[str],
) -> list[str]:
    """Upload the published ref's merged bundle before deleting old bundles.

    Include ``HEAD`` only when it names that same commit; an unrelated checked-out
    branch must never enter the ticket bundle. The remote advertises HEAD separately.
    """
    merged_key = f"{s3.prefix}/{ref}/{merged_sha}.bundle"
    bundle_refs = [f"refs/heads/{ref}"]
    if _git(base_path, "rev-parse", "--verify", "--quiet", "HEAD").stdout.strip() == merged_sha:
        bundle_refs.insert(0, "HEAD")

    with tempfile.NamedTemporaryFile(suffix=".bundle", dir=base_path, delete=False) as tmp:
        bundle_path = tmp.name
    try:
        create = _git(base_path, "bundle", "create", bundle_path, *bundle_refs)
        if create.returncode != 0:
            # A persistent bundle read failure follows a successful merge; diagnose
            # the object store rather than treating it as a heads conflict.
            hint = None
            if is_transient_object_read_error(create.stderr or create.stdout or ""):
                hint = (
                    "git could not read an object it had just resolved, and the read did not "
                    "recover on retry; the merge itself succeeded. Re-run the heal, and if it "
                    "persists check the tracker's object store with: git fsck"
                )
            raise S3DoctorConflict(
                f"could not bundle merged tip {merged_sha} for ref {ref}: {create.stderr}",
                hint=hint,
            )
        with open(bundle_path, "rb") as fh:
            s3.s3.put_object(Bucket=s3.bucket, Key=merged_key, Body=fh.read())
    finally:
        try:
            os.unlink(bundle_path)
        except OSError:
            pass

    # Delete originals — NEVER the newly-written merged bundle. Let a delete raise (the
    # merged tip is already durable).
    deleted_keys: list[str] = []
    for key in original_keys:
        if key == merged_key:
            continue
        s3.s3.delete_object(Bucket=s3.bucket, Key=key)
        deleted_keys.append(key)
    return deleted_keys
