"""Shared harness for the S3 auto-doctor oracle (story 0289).

The doctor heals the git-remote-s3 "multiple bundles for one ref" state losslessly. Its only
S3-specific surface is routed through git-remote-s3's own ``S3Remote`` object; the heal is
injected with an ``s3remote_factory`` so the oracle can drive it against a **real** object store
modelled as a local directory of **real** ``git bundle`` files — no AWS, no new hard test dep.

``FakeS3Remote`` implements exactly the surface ``heal_multi_bundle`` binds:

* ``bucket`` / ``prefix`` attributes and a ``.s3`` client exposing ``put_object`` /
  ``delete_object`` / ``download_file`` / ``list_objects_v2`` over ``objdir``;
* ``get_bundles_for_ref(ref)`` -> ``[{"Key": ...}]`` (bundles only, mirroring the helper's
  PROTECTED/.zip/.lock exclusions);
* ``acquire_lock(ref)`` -> lock key or ``None`` (atomic ``O_EXCL`` create, TTL steal), and
  ``release_lock(ref, key)`` — the helper's own ``IfNoneMatch`` semantics, filesystem-modelled
  so the multi-process race test is real;
* ``init_remote_head(ref)`` — idempotent HEAD marker.

Object keys mirror the helper's layout: ``<prefix>/<ref>/<sha>.bundle``,
``<prefix>/<ref>/LOCK#.lock``, ``<prefix>/HEAD``.
"""

from __future__ import annotations

import errno
import os
import subprocess
import time
from pathlib import Path
from types import SimpleNamespace

from _subprocess_env import subprocess_env


def git(cwd: str | Path, *args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Run git in ``cwd`` with an isolated, hook-free, identity-stamped environment."""
    env = subprocess_env(
        {
            "GIT_CONFIG_GLOBAL": "/dev/null",
            "GIT_CONFIG_SYSTEM": "/dev/null",
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@t",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@t",
        }
    )
    return subprocess.run(
        ["git", *args],
        cwd=str(cwd),
        env=env,
        text=True,
        capture_output=True,
        check=check,
    )


def init_repo(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    git(path, "init", "-q", "-b", "tickets")
    return path


def commit_file(repo: Path, name: str, content: str, message: str) -> str:
    (repo / name).write_text(content)
    git(repo, "add", name)
    git(repo, "commit", "-q", "-m", message)
    return git(repo, "rev-parse", "HEAD").stdout.strip()


class _FakeS3Client:
    """The boto3-``.s3`` slice the doctor touches, over a local object directory."""

    def __init__(self, objdir: Path) -> None:
        self._root = objdir

    def _p(self, key: str) -> Path:
        return self._root / key

    def put_object(self, Bucket: str, Key: str, Body: object) -> None:
        dst = self._p(Key)
        dst.parent.mkdir(parents=True, exist_ok=True)
        if hasattr(Body, "read"):
            data = Body.read()
        elif isinstance(Body, (bytes, bytearray)):
            data = bytes(Body)
        else:
            data = str(Body).encode()
        dst.write_bytes(data)

    def delete_object(self, Bucket: str, Key: str) -> None:
        self._p(Key).unlink(missing_ok=True)

    def download_file(self, Bucket: str, Key: str, Filename: str) -> None:
        Path(Filename).write_bytes(self._p(Key).read_bytes())

    def list_objects_v2(self, Bucket: str, Prefix: str) -> dict:
        base = self._root
        contents = []
        for p in sorted(base.rglob("*")):
            if p.is_file():
                key = str(p.relative_to(base))
                if key.startswith(Prefix):
                    contents.append({"Key": key})
        return {"Contents": contents}


class FakeS3Remote:
    """An in-process stand-in for git_remote_s3.S3Remote over ``objdir`` (real git bundles)."""

    def __init__(
        self,
        objdir: Path,
        *,
        bucket: str = "test-bucket",
        prefix: str = "tickets",
        lock_ttl_seconds: int = 60,
    ) -> None:
        self.objdir = Path(objdir)
        self.objdir.mkdir(parents=True, exist_ok=True)
        self.bucket = bucket
        self.prefix = prefix
        self.lock_ttl_seconds = lock_ttl_seconds
        self.s3 = _FakeS3Client(self.objdir)

    # --- enumeration (mirrors the helper's exclusions) ---
    def get_bundles_for_ref(self, remote_ref: str) -> list[dict]:
        pfx = f"{self.prefix}/{remote_ref}/"
        out = []
        for c in self.s3.list_objects_v2(Bucket=self.bucket, Prefix=pfx).get("Contents", []):
            k = c["Key"]
            if "PROTECTED#" in k or k.endswith(".zip") or "/LOCKS/" in k or k.endswith(".lock"):
                continue
            out.append(c)
        return out

    # --- the helper's IfNoneMatch per-ref lock, filesystem-modelled (atomic O_EXCL) ---
    def _lock_path(self, remote_ref: str) -> Path:
        return self.objdir / self.prefix / remote_ref / "LOCK#.lock"

    def acquire_lock(self, remote_ref: str):
        lp = self._lock_path(remote_ref)
        lp.parent.mkdir(parents=True, exist_ok=True)
        key = f"{self.prefix}/{remote_ref}/LOCK#.lock"
        try:
            fd = os.open(str(lp), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.close(fd)
            return key
        except OSError as exc:
            if exc.errno != errno.EEXIST:
                raise
            age = time.time() - lp.stat().st_mtime
            if age > self.lock_ttl_seconds:
                lp.unlink(missing_ok=True)
                try:
                    fd = os.open(str(lp), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                    os.close(fd)
                    return key
                except OSError:
                    return None
            return None

    def release_lock(self, remote_ref: str, lock_key: str) -> None:
        self._lock_path(remote_ref).unlink(missing_ok=True)

    def init_remote_head(self, ref: str) -> None:
        head = self.objdir / self.prefix / "HEAD"
        if not head.exists():
            head.parent.mkdir(parents=True, exist_ok=True)
            head.write_text(ref)


def seed_bundle(remote: FakeS3Remote, source_repo: Path, ref: str, branch: str) -> str:
    """Create a real git bundle of ``branch`` from ``source_repo`` and store it under the ref's
    bundle key ``<prefix>/<ref>/<sha>.bundle``. Returns the bundled tip sha."""
    sha = git(source_repo, "rev-parse", branch).stdout.strip()
    bundle_path = remote.objdir / f"tmp-{sha}.bundle"
    git(source_repo, "bundle", "create", str(bundle_path), f"refs/heads/{branch}")
    key = f"{remote.prefix}/{ref}/{sha}.bundle"
    remote.s3.put_object(Bucket=remote.bucket, Key=key, Body=bundle_path.read_bytes())
    bundle_path.unlink(missing_ok=True)
    return sha


def bundle_keys(remote: FakeS3Remote, ref: str) -> list[str]:
    return [c["Key"] for c in remote.get_bundles_for_ref(ref)]


def clone_from_single_bundle(remote: FakeS3Remote, ref: str, dest: Path) -> Path:
    """Download the ONE remaining bundle for ``ref`` and clone it — asserts single-bundle."""
    keys = bundle_keys(remote, ref)
    assert len(keys) == 1, f"expected exactly one bundle, got {keys}"
    bfile = dest.parent / "healed.bundle"
    remote.s3.download_file(Bucket=remote.bucket, Key=keys[0], Filename=str(bfile))
    git(dest.parent, "clone", "-q", str(bfile), dest.name)
    return dest


def reachable(repo: Path, tip_ref: str, sha: str) -> bool:
    """True if ``sha`` is reachable (an ancestor or equal) from ``tip_ref`` in ``repo``."""
    r = git(repo, "merge-base", "--is-ancestor", sha, tip_ref, check=False)
    return r.returncode == 0


def healed_tip(remote: FakeS3Remote, ref: str, scratch: Path) -> tuple[Path, str]:
    """Clone the single healed bundle and return (clone_path, its tip sha)."""
    dest = scratch / "verify-clone"
    clone_from_single_bundle(remote, ref, dest)
    tip = git(dest, "rev-parse", "HEAD").stdout.strip()
    return dest, tip


def make_factory(remote: FakeS3Remote):
    """An ``s3remote_factory`` (url -> S3Remote-like) that ignores url and returns ``remote``."""
    return lambda _url: remote


def default_head_env() -> SimpleNamespace:
    return SimpleNamespace()
