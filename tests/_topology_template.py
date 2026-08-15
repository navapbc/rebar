"""Safe copy-on-test helpers for expensive git-backed fixture topologies.

The source topology is built once per pytest worker.  Each test receives a real
filesystem copy whose git worktree pointers and local remotes have been rebased
from the template root to the copy root.  ``shutil.copytree`` deliberately copies
object bytes rather than hardlinking them, so refs and object stores stay isolated.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import uuid
from pathlib import Path
from typing import TypeAlias

_IDENTITY_FILES = ((".env-id", 0o644), (".signing-key", 0o600))
_PathRewrite: TypeAlias = tuple[bytes, bytes]


def worktree_paths(repo: Path) -> list[Path]:
    """Return every worktree path git associates with ``repo``."""
    out = subprocess.run(
        ["git", "-C", str(repo), "worktree", "list", "--porcelain"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    return [
        Path(line.split(" ", 1)[1]) for line in out.splitlines() if line.startswith("worktree ")
    ]


def assert_store_self_contained(repo: Path) -> None:
    """Fail unless all worktrees associated with ``repo`` live below it."""
    root = repo.resolve()
    stray = [
        path
        for path in worktree_paths(repo)
        if root not in (path.resolve(), *path.resolve().parents)
    ]
    if stray:
        raise AssertionError(
            f"store at {root} references worktrees outside itself: {stray}. "
            "A copied store was not re-pointed at itself; it is sharing another "
            "store's object database and refs."
        )


def _store_repos(topology: Path) -> list[Path]:
    return sorted(path.parent for path in topology.rglob(".tickets-tracker") if path.is_dir())


def _assert_no_object_alternates(topology: Path) -> None:
    alternates = [
        path
        for path in topology.rglob("alternates")
        if path.is_file()
        and path.parent.name == "info"
        and path.parent.parent.name == "objects"
        and path.read_bytes().strip()
    ]
    if alternates:
        raise AssertionError(
            f"topology at {topology.resolve()} uses unsupported Git object alternates: "
            f"{alternates}. Copying it would keep reading another topology's object store."
        )


def _inside_git_object_store(path: Path) -> bool:
    for parent in path.parents:
        if parent.name != "objects":
            continue
        git_dir = parent.parent
        if git_dir.name == ".git" or (
            (git_dir / "HEAD").is_file() and (git_dir / "config").is_file()
        ):
            return True
    return False


def _git_output(repo: Path, *args: str) -> str | None:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=False,
        capture_output=True,
        text=True,
    )
    return completed.stdout if completed.returncode == 0 else None


def _template_may_have_remotes(template: Path) -> bool:
    """Cheaply reject the overwhelmingly common remote-free template."""
    git_marker = template / ".git"
    config = git_marker / "config" if git_marker.is_dir() else template / "config"
    if config.is_file():
        data = config.read_bytes()
        return b'[remote "' in data or b"[include" in data
    return git_marker.is_file()


def _refs_signature(repo: Path) -> tuple[str, str] | None:
    refs = _git_output(repo, "for-each-ref", "--format=%(refname) %(objectname)")
    if refs is None:
        return None
    head = _git_output(repo, "symbolic-ref", "-q", "HEAD")
    return refs, head or ""


def _local_sibling_rewrites(template: Path, destination: Path) -> list[_PathRewrite]:
    """Map only proven copied siblings named by real Git remote metadata."""
    if not _template_may_have_remotes(template):
        return []
    if _git_output(template, "rev-parse", "--absolute-git-dir") is None:
        return []

    template_root = template.resolve()
    source_parent = template_root.parent
    destination_parent = destination.resolve().parent
    rewrites: dict[bytes, bytes] = {}

    remotes = _git_output(template, "remote")
    if remotes is None:
        return []
    for remote in remotes.splitlines():
        urls = _git_output(template, "remote", "get-url", "--all", remote)
        if urls is None:
            continue
        for url in urls.splitlines():
            # Only a literal absolute filesystem path is safely relocatable.
            # Relative paths already retain their sibling relationship; URLs,
            # scp syntax, and non-local remotes must not be guessed.
            source_remote = Path(url)
            if not source_remote.is_absolute() or "://" in url:
                continue
            source_remote = source_remote.resolve()
            if template_root in (source_remote, *source_remote.parents):
                continue
            try:
                relative = source_remote.relative_to(source_parent)
            except ValueError:
                continue
            copied_remote = destination_parent / relative
            if not copied_remote.exists():
                continue
            source_signature = _refs_signature(source_remote)
            if source_signature is None or _refs_signature(copied_remote) != source_signature:
                continue
            rewrites[os.fsencode(url)] = os.fsencode(copied_remote.resolve())
    return list(rewrites.items())


def _rewrite_embedded_paths(topology: Path, rewrites: list[_PathRewrite]) -> None:
    for path in topology.rglob("*"):
        if path.is_symlink():
            target = os.readlink(path)
            rewritten_bytes = os.fsencode(target)
            for source, destination in rewrites:
                rewritten_bytes = rewritten_bytes.replace(source, destination)
            rewritten = os.fsdecode(rewritten_bytes)
            if rewritten != target:
                path.unlink()
                path.symlink_to(rewritten)
            continue
        if not path.is_file() or _inside_git_object_store(path):
            continue
        data = path.read_bytes()
        rewritten = data
        for source, destination in rewrites:
            rewritten = rewritten.replace(source, destination)
        if rewritten != data:
            path.write_bytes(rewritten)


def _remint_store_identities(topology: Path) -> None:
    for repo in _store_repos(topology):
        tracker = repo / ".tickets-tracker"
        for relative, mode in _IDENTITY_FILES:
            path = tracker / relative
            if path.exists():
                path.write_text(f"{uuid.uuid4()}\n", encoding="utf-8")
                path.chmod(mode)
        opcert_key = tracker / ".opcert-key"
        if opcert_key.exists():
            opcert_key.unlink()
            opcert_key.with_name(f"{opcert_key.name}.pub").unlink(missing_ok=True)


def _assert_source_paths_absent(topology: Path, rewrites: list[_PathRewrite]) -> None:
    source_paths = {source for source, _destination in rewrites}
    references: list[Path] = []
    for path in topology.rglob("*"):
        if path.is_symlink():
            if any(source in os.fsencode(os.readlink(path)) for source in source_paths):
                references.append(path)
        elif (
            path.is_file()
            and not _inside_git_object_store(path)
            and any(source in path.read_bytes() for source in source_paths)
        ):
            references.append(path)
    if references:
        raise AssertionError(
            f"copied topology at {topology} still embeds source topology paths: {references}"
        )


def clone_topology_template(template: Path, destination: Path) -> Path:
    """Copy and isolate an arbitrary local git/rebar fixture topology.

    The topology may contain multiple ordinary repositories, bare sibling
    remotes, and repositories with rebar's linked ``.tickets-tracker`` worktree.
    Absolute paths below the template root are rebased in git metadata, every
    rebar identity is reminted, and both source and copy are checked through
    git's own worktree registry.
    """
    _assert_no_object_alternates(template)
    for repo in _store_repos(template):
        assert_store_self_contained(repo)

    rewrites: list[_PathRewrite] = [
        (os.fsencode(template.resolve()), os.fsencode(destination.resolve())),
        *_local_sibling_rewrites(template, destination),
    ]
    shutil.copytree(template, destination, symlinks=True)
    _rewrite_embedded_paths(destination, rewrites)
    _remint_store_identities(destination)

    _assert_source_paths_absent(destination, rewrites)
    for repo in _store_repos(destination):
        assert_store_self_contained(repo)
    return destination
