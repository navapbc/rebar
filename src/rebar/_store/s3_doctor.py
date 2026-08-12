"""Non-interactive S3 "auto-doctor": heal git-remote-s3's multi-bundle state LOSSLESSLY.

When the ticket store's remote is an ``s3://`` URL served by ``git-remote-s3``, concurrent or
legacy pushes can leave TWO ``<sha>.bundle`` objects for one ref. ``git ls-remote`` then returns
the ref at two SHAs, a fresh clone hard-fails (``multiple updates for ref not allowed``), and the
push path stalls. Upstream's ``git-s3 doctor`` is interactive-only AND discards a divergent head
(data loss). :func:`heal_multi_bundle` instead folds every head into HEAD by iterated 2-parent
merges under rebar's write lock, writes the merged tip as a NEW bundle FIRST, then deletes the
originals — so at every instant ≥1 bundle carrying the full merged history exists remotely.

This module is OPTIONAL and import-safe: ``git_remote_s3`` / boto3 are imported ONLY lazily
inside functions, guarded by :func:`rebar._optional.require_s3_helper`.
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
        self.hint = hint or "tickets store heads conflict; run: rebar fsck-recover"


# raw-git-ok: locked store seam internal
def _git(base: str, *args: str) -> subprocess.CompletedProcess:
    """Run one doctor git invocation through rebar's SHARED self-healing git seam.

    Routed through :func:`gitutil.run_git_write` rather than raw ``run_git`` (bug
    wrongful-chemic-squeaker): the doctor was the one store git caller outside that seam, so
    a transient runner-FS read blip that every other store write rides out unassisted instead
    aborted the whole heal — three times on CI. Adopting the seam is reuse, not new policy:
    the doctor inherits the per-store advisory git-op lock, the bounded index/ref-lock retry
    and the transient read-fault retry from the one implementation, and keeps its own
    :data:`_GIT_TIMEOUT` watchdog. Every op reached through here is idempotent — reads,
    merges under the write lock, disposable scratch-ref updates, and a bundle write to a
    caller-owned temp file — which is the precondition the retry wrappers document.
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
    """Losslessly collapse a ref's multiple bundles into one merged bundle.

    ``base_path`` is the local tracker git repo where the merge happens; ``remote`` is the git
    remote NAME (its URL is resolved via ``git remote get-url``); ``ref`` is the remote ref.
    ``s3remote_factory`` is the single injection seam: ``(remote_url) -> S3Remote-like``.
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
    """Fold every head into HEAD by iterated 2-parent merges under rebar's write lock.

    Reuses rebar's existing merge policy (the tickets branch's ``.gitattributes`` maps
    ``.bridge_state/* merge=ours``; UUID-named event dirs never collide) so a plain multi-parent
    merge is lossless. On a real conflict: abort and raise :class:`S3DoctorConflict`.
    """
    with _lock.write_lock(base_path, timeout=_WRITE_LOCK_TIMEOUT, attempts=1, dual_window=True):
        for head_ref in head_refs:
            # Already reachable from HEAD → nothing to merge for this head.
            if _git(base_path, "merge-base", "--is-ancestor", head_ref, "HEAD").returncode == 0:
                continue
            extra: list[str] = []
            if _git(base_path, "merge-base", "HEAD", head_ref).stdout.strip() == "":
                extra = ["--allow-unrelated-histories"]
            merge = _git(
                base_path,
                "merge",
                *extra,
                head_ref,
                "--no-edit",
                "-m",
                f"Merge {head_ref} (s3 doctor auto-heal of ref {ref})",
            )
            if merge.returncode != 0:
                _git(base_path, "merge", "--abort")
                raise S3DoctorConflict(
                    f"tickets store heads conflict merging {head_ref} for ref {ref}: {merge.stderr}"
                )
        return _git(base_path, "rev-parse", "HEAD").stdout.strip()


def _publish_and_prune(
    base_path: str,
    ref: str,
    s3: Any,
    merged_sha: str,
    original_keys: list[str],
) -> list[str]:
    """Write the merged tip as a NEW bundle FIRST, then delete the original bundles."""
    merged_key = f"{s3.prefix}/{ref}/{merged_sha}.bundle"

    # Point a local branch <ref> at merged_sha so the bundle carries refs/heads/<ref>. When
    # <ref> is the checked-out branch it already points at merged_sha (we merged into HEAD),
    # and git refuses to force-update a checked-out branch — so only update when it differs.
    current_branch = _git(base_path, "symbolic-ref", "--quiet", "--short", "HEAD").stdout.strip()
    if current_branch != ref:
        update = _git(base_path, "branch", "-f", ref, merged_sha)
        if update.returncode != 0:
            raise S3DoctorConflict(
                f"could not point branch {ref} at merged tip {merged_sha}: {update.stderr}"
            )

    with tempfile.NamedTemporaryFile(suffix=".bundle", dir=base_path, delete=False) as tmp:
        bundle_path = tmp.name
    try:
        create = _git(base_path, "bundle", "create", bundle_path, "HEAD", f"refs/heads/{ref}")
        if create.returncode != 0:
            # A bundle failure that survived the seam's retries is NOT a heads conflict, so
            # the default hint's "run rebar fsck-recover" would be the wrong tool: the merge
            # already succeeded and it is the object store that could not be READ.
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
