#!/usr/bin/env python3
"""Prepare an offline, verifiable backup before reclaiming tickets history.

The tool deliberately uses only local Git plumbing plus ``ls-remote``.  It never
pushes or alters a remote ref; its short-lived local refs exist solely to name the
objects placed in the bundle.
"""

from __future__ import annotations

import argparse
import json
import secrets
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from reclaim_bridge_history import ReclaimError, is_partial_clone, ref_name, run_git

HELPER_PREFIX = "refs/reclaim-backup/"


@dataclass(frozen=True)
class RemoteRef:
    """One canonical remote head or tag, retaining an annotated tag's peel."""

    name: str
    direct_oid: str
    peeled_oid: str | None = None

    @property
    def target_oid(self) -> str:
        return self.peeled_oid or self.direct_oid


def parse_args(argv: Sequence[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", required=True, type=Path)
    parser.add_argument("--old-tip", required=True)
    parser.add_argument("--bundle", required=True, type=Path)
    parser.add_argument("--manifest", required=True, type=Path)
    parser.add_argument("--pin-ref", action="append", default=[])
    return parser.parse_args(argv)


def _git_status(repo: Path, args: Sequence[str]) -> subprocess.CompletedProcess[bytes]:
    """Run a Git predicate whose nonzero exit code may be meaningful."""
    return subprocess.run(["git", "-C", str(repo), *args], capture_output=True)


def _require_success(completed: subprocess.CompletedProcess[bytes], description: str) -> None:
    if completed.returncode:
        detail = completed.stderr.decode(errors="replace").strip()
        raise ReclaimError(f"{description}: {detail}")


def _run_bare_git(repo: Path, args: Sequence[str]) -> subprocess.CompletedProcess[bytes]:
    """Run Git against an explicitly selected bare repository."""
    completed = subprocess.run(
        ["git", "--git-dir", str(repo), *args],
        capture_output=True,
    )
    _require_success(completed, f"git {' '.join(args)} failed")
    return completed


def _remote_snapshot(repo: Path) -> bytes:
    return run_git(repo, ["ls-remote", "--heads", "--tags", "origin"]).stdout


def _parse_remote_refs(snapshot: bytes) -> list[RemoteRef]:
    """Collapse ``^{}`` rows while retaining both annotated-tag object IDs."""
    direct: dict[str, str] = {}
    order: list[str] = []
    peeled: dict[str, str] = {}
    for line in snapshot.splitlines():
        try:
            oid_bytes, name_bytes = line.split(b"\t", 1)
            oid = oid_bytes.decode("ascii")
            name = name_bytes.decode("utf-8", "surrogateescape")
        except ValueError as exc:
            raise ReclaimError("malformed git ls-remote snapshot") from exc
        if name.endswith("^{}"):
            base = name[:-3]
            if not base.startswith("refs/tags/"):
                raise ReclaimError(f"unexpected peeled remote ref: {name}")
            peeled[base] = oid
            continue
        if not name.startswith(("refs/heads/", "refs/tags/")):
            raise ReclaimError(f"unexpected remote ref: {name}")
        if name in direct:
            raise ReclaimError(f"duplicate remote ref in snapshot: {name}")
        direct[name] = oid
        order.append(name)
    if not direct:
        raise ReclaimError("origin has no heads or tags to snapshot")
    dangling = peeled.keys() - direct.keys()
    if dangling:
        raise ReclaimError(f"peeled tag lacks direct row: {sorted(dangling)[0]}")
    return [RemoteRef(name, direct[name], peeled.get(name)) for name in order]


def _commit_oid(repo: Path, ref: str, *, label: str) -> str:
    """Resolve a ref to a commit and explain a missing/non-commit pin distinctly."""
    completed = _git_status(repo, ["rev-parse", "--verify", f"{ref}^{{commit}}"])
    if completed.returncode == 0:
        return completed.stdout.decode().strip()
    object_type = _git_status(repo, ["cat-file", "-t", ref])
    if object_type.returncode == 0:
        raise ReclaimError(f"{label} is not a commit: {ref}")
    raise ReclaimError(f"{label} is missing locally: {ref}; use a fresh unfiltered clone")


def _relationship(repo: Path, old_tip: str, ref: RemoteRef) -> str:
    """Classify the effective (peeled when present) remote object against old tip."""
    target = ref.target_oid
    object_type = _git_status(repo, ["cat-file", "-t", target])
    if object_type.returncode:
        raise ReclaimError(
            f"remote ref {ref.name} is missing locally; use a fresh unfiltered clone"
        )
    if object_type.stdout.strip() != b"commit":
        return "non-commit"
    if target == old_tip:
        return "same"
    old_ancestor = _git_status(repo, ["merge-base", "--is-ancestor", old_tip, target])
    if old_ancestor.returncode == 0:
        return "old-tip-is-ancestor"
    if old_ancestor.returncode != 1:
        _require_success(old_ancestor, "could not compare remote ref ancestry")
    ref_ancestor = _git_status(repo, ["merge-base", "--is-ancestor", target, old_tip])
    if ref_ancestor.returncode == 0:
        return "ref-is-ancestor"
    if ref_ancestor.returncode != 1:
        _require_success(ref_ancestor, "could not compare remote ref ancestry")
    merge_base = _git_status(repo, ["merge-base", old_tip, target])
    if merge_base.returncode == 0:
        return "diverged-with-merge-base"
    if merge_base.returncode == 1:
        return "unrelated"
    _require_success(merge_base, "could not find remote ref merge base")
    raise AssertionError("unreachable")


def _registered_worktrees(repo: Path) -> list[Path]:
    output = run_git(repo, ["worktree", "list", "--porcelain"]).stdout.decode()
    return [
        _repo_relative_path(repo, line.removeprefix("worktree "))
        for line in output.splitlines()
        if line.startswith("worktree ")
    ]


def _is_within(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _repo_relative_path(repo: Path, value: str) -> Path:
    """Resolve Git path output, which may be relative to the source worktree."""
    path = Path(value)
    return (path if path.is_absolute() else repo / path).resolve()


def _validate_artifact_paths(repo: Path, bundle: Path, manifest: Path) -> tuple[Path, Path]:
    """Reject in-repository outputs before creating any output directory."""
    bundle = bundle.resolve()
    manifest = manifest.resolve()
    if bundle == manifest:
        raise ReclaimError("bundle and manifest paths must differ")
    for path in (bundle, manifest):
        if path.exists() or path.is_symlink():
            raise ReclaimError(f"refusing to overwrite existing artifact: {path}")

    git_dirs = {
        _repo_relative_path(
            repo, run_git(repo, ["rev-parse", "--absolute-git-dir"]).stdout.decode().strip()
        ),
        _repo_relative_path(
            repo, run_git(repo, ["rev-parse", "--git-common-dir"]).stdout.decode().strip()
        ),
    }
    worktrees = [repo, *_registered_worktrees(repo)]
    for path in (bundle, manifest):
        if any(_is_within(path, git_dir) for git_dir in git_dirs):
            raise ReclaimError(f"artifact path is inside a Git directory: {path}")
        if any(_is_within(path, worktree) for worktree in worktrees):
            raise ReclaimError(f"artifact path is inside a registered worktree: {path}")
    return bundle, manifest


def _assert_safe_clone(repo: Path) -> None:
    if not repo.is_dir() or not (repo / ".git").exists():
        raise ReclaimError(f"not a Git worktree: {repo}")
    if run_git(repo, ["status", "--porcelain"]).stdout:
        raise ReclaimError("backup source worktree is not clean")
    if run_git(repo, ["rev-parse", "--is-shallow-repository"]).stdout.strip() == b"true":
        raise ReclaimError("shallow clone is unsafe; use a fresh unfiltered clone")
    if is_partial_clone(repo):
        raise ReclaimError("partial/promisor clone is unsafe; use a fresh unfiltered clone")
    existing = run_git(repo, ["for-each-ref", "--format=%(refname)", HELPER_PREFIX]).stdout
    if existing:
        raise ReclaimError(
            "refs/reclaim-backup/ already exists; use a fresh unfiltered clone after cleanup"
        )


def _resolve_pins(repo: Path, remote_refs: list[RemoteRef], pins: Sequence[str]) -> list[str]:
    remote_by_name = {ref.name: ref for ref in remote_refs}
    resolved: list[str] = []
    for requested in pins:
        canonical = requested if requested in remote_by_name else ref_name(repo, requested)
        if not canonical.startswith("refs/"):
            raise ReclaimError(f"--pin-ref must name a ref: {requested}")
        remote = remote_by_name.get(canonical)
        if remote is not None:
            resolved.append(_commit_oid(repo, remote.target_oid, label=f"--pin-ref {canonical}"))
        else:
            resolved.append(_commit_oid(repo, canonical, label=f"--pin-ref {canonical}"))
    return resolved


def _create_helper_refs(
    repo: Path,
    namespace: str,
    old_tip: str,
    pins: Sequence[str],
    bundle_refs: dict[str, str],
) -> None:
    bundle_refs[f"{namespace}/old-tip"] = old_tip
    for index, oid in enumerate(pins, 1):
        bundle_refs[f"{namespace}/pin-{index}"] = oid
    for name, oid in bundle_refs.items():
        run_git(repo, ["update-ref", name, oid])


def _cleanup_helper_refs(repo: Path | None, refs: Sequence[str]) -> None:
    if repo is None:
        return
    for ref in refs:
        _git_status(repo, ["update-ref", "-d", ref])


def _bundle_heads(repo: Path, bundle: Path) -> dict[str, str]:
    output = run_git(repo, ["bundle", "list-heads", str(bundle)]).stdout.decode()
    heads: dict[str, str] = {}
    for line in output.splitlines():
        try:
            oid, name = line.split(" ", 1)
        except ValueError as exc:
            raise ReclaimError("malformed git bundle list-heads output") from exc
        heads[name] = oid
    return heads


def _verify_restore(repo: Path, bundle: Path, bundle_refs: dict[str, str]) -> None:
    run_git(repo, ["bundle", "verify", str(bundle)])
    if _bundle_heads(repo, bundle) != bundle_refs:
        raise ReclaimError("bundle refs do not exactly match the prepared backup refs")
    with tempfile.TemporaryDirectory(prefix="reclaim-backup-restore-") as temporary:
        restored = Path(temporary) / "restore.git"
        initialized = subprocess.run(["git", "init", "--bare", str(restored)], capture_output=True)
        _require_success(initialized, "could not initialize throwaway restore repository")
        for bundle_ref, expected_oid in bundle_refs.items():
            target_ref = "refs/restored/" + bundle_ref.removeprefix(HELPER_PREFIX)
            _run_bare_git(restored, ["fetch", str(bundle), f"{bundle_ref}:{target_ref}"])
            actual = _run_bare_git(restored, ["rev-parse", target_ref]).stdout.decode().strip()
            if actual != expected_oid:
                raise ReclaimError(f"restored {bundle_ref} differs from its bundled OID")


def _manifest(
    old_tip: str, remote_refs: list[RemoteRef], repo: Path
) -> list[dict[str, str | None]]:
    return [
        {
            "ref": ref.name,
            "direct_oid": ref.direct_oid,
            "peeled_oid": ref.peeled_oid,
            "relationship": _relationship(repo, old_tip, ref),
        }
        for ref in remote_refs
    ]


def prepare(args: argparse.Namespace) -> None:
    repo: Path | None = None
    helper_refs: dict[str, str] = {}
    bundle: Path | None = None
    manifest: Path | None = None
    complete = False
    try:
        repo = args.repo.resolve()
        _assert_safe_clone(repo)
        bundle, manifest = _validate_artifact_paths(repo, args.bundle, args.manifest)
        initial_snapshot = _remote_snapshot(repo)
        remote_refs = _parse_remote_refs(initial_snapshot)
        remote_by_name = {ref.name: ref for ref in remote_refs}
        tickets = remote_by_name.get("refs/heads/tickets")
        if tickets is None:
            raise ReclaimError("origin snapshot has no refs/heads/tickets")
        if tickets.direct_oid != args.old_tip:
            raise ReclaimError("--old-tip differs from the initial live refs/heads/tickets OID")
        old_tip = _commit_oid(repo, args.old_tip, label="--old-tip")
        pins = _resolve_pins(repo, remote_refs, args.pin_ref)

        bundle.parent.mkdir(parents=True, exist_ok=True)
        manifest.parent.mkdir(parents=True, exist_ok=True)
        namespace = f"{HELPER_PREFIX}{secrets.token_hex(12)}"
        _create_helper_refs(repo, namespace, old_tip, pins, helper_refs)
        run_git(repo, ["bundle", "create", str(bundle), *helper_refs])
        _verify_restore(repo, bundle, helper_refs)
        manifest.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "old_tip": old_tip,
                    "remote_refs": _manifest(old_tip, remote_refs, repo),
                    "bundle_refs": helper_refs,
                },
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        if _remote_snapshot(repo) != initial_snapshot:
            raise ReclaimError("remote heads/tags snapshot changed before backup completion")
        complete = True
    finally:
        _cleanup_helper_refs(repo, list(helper_refs))
        if not complete:
            for artifact in (bundle, manifest):
                if artifact is not None:
                    try:
                        artifact.unlink()
                    except FileNotFoundError:
                        pass


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(sys.argv[1:] if argv is None else argv)
    try:
        prepare(args)
    except ReclaimError as exc:
        sys.stderr.write(f"BACKUP FAILED: {exc}\n")
        return 1
    sys.stdout.write("BACKUP READY\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
