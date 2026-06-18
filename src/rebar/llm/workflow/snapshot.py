"""Hardened git-ref filesystem snapshot for agentic steps (WS-D2).

An agent step often needs to read the repository at a CALLER-CHOSEN git ref (e.g.
the commit a code-review workflow targets), not the dirty working tree. This builds
a safe, immutable, read-only view of the repo at a resolved commit:

  ``git rev-parse <ref>^{commit}`` → a full SHA → ``git archive`` that SHA →
  a HARDENED tar extract → ``chmod`` the tree read-only.

Safety properties:
  * **Immutable input** — the snapshot is bound to the resolved SHA, never the
    mutable ref/branch, so two steps at "the same ref" see byte-identical trees.
  * **No .git** — ``git archive`` emits only tracked content at the SHA, so an
    agent's read-only/no-git tools cannot reach repo history or hooks.
  * **Hardened extraction** — extraction uses the stdlib ``data`` tar filter
    (rejects absolute paths, ``..`` escapes, and links/symlinks pointing outside
    the destination), plus a total-size guard, so a malicious tar cannot write
    outside the snapshot.
  * **Cache by SHA** — snapshots live at ``<repo>/.rebar/run_snapshots/<sha>`` and
    are reused across steps/runs; the WS-C3 TTL sweep
    (:func:`rebar.llm.workflow.executor.sweep_orphan_snapshots`) GCs stale ones.

Documented git-archive behavior (callers should know): ``.gitattributes``
``export-ignore`` paths are omitted and ``export-subst`` is applied (git archive
does this natively); **submodule** contents are NOT included (archive stops at the
gitlink); **Git-LFS** files appear as their pointer text, not the smudged content
(no LFS smudge runs). Untracked/gitignored files are absent by construction.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path

from rebar.llm.errors import WorkflowError

# Total extracted-bytes ceiling — a snapshot is a source tree, not a data lake;
# this bounds a pathological/hostile archive.
DEFAULT_MAX_SNAPSHOT_BYTES = 512 * 1024 * 1024  # 512 MiB


class SnapshotError(WorkflowError):
    """Building a git-ref snapshot failed (bad ref, git error, oversize, unsafe tar)."""


def _snapshot_root(repo_root: str | None) -> Path:
    # Mirror executor.snapshot_root without importing it (avoid a cycle); the WS-C3
    # sweep GCs this same directory.
    base = Path(repo_root) if repo_root else Path.cwd()
    return base / ".rebar" / "run_snapshots"


def resolve_sha(ref: str, repo_root: str | None = None) -> str:
    """Resolve ``ref`` to a full commit SHA (``<ref>^{commit}``).

    Pins the snapshot to an immutable commit object, never a moving branch/tag.
    Raises :class:`SnapshotError` if the ref doesn't resolve to a commit."""
    root = str(repo_root) if repo_root else "."
    proc = subprocess.run(
        ["git", "-C", root, "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}"],
        capture_output=True,
        text=True,
    )
    sha = proc.stdout.strip()
    if proc.returncode != 0 or not sha:
        raise SnapshotError(
            f"cannot resolve git ref {ref!r} to a commit: {proc.stderr.strip() or 'no such ref'}"
        )
    return sha


def _hardened_filter(max_bytes: int):
    """A tarfile extraction filter: the stdlib ``data`` filter (rejects absolute
    paths, ``..`` escapes, and escaping links) plus a cumulative size guard."""
    seen = {"total": 0}

    def _filter(member: tarfile.TarInfo, dest_path: str) -> tarfile.TarInfo | None:
        # data_filter raises on absolute paths / .. traversal / unsafe links.
        member = tarfile.data_filter(member, dest_path)
        seen["total"] += max(0, member.size)
        if seen["total"] > max_bytes:
            raise SnapshotError(
                f"snapshot exceeds the {max_bytes}-byte cap (extracted "
                f"{seen['total']} bytes); refusing to continue"
            )
        return member

    return _filter


def _chmod_readonly(root: Path) -> None:
    """Make the extracted tree read-only (files r-x/r--, dirs r-x) so a read-only
    step cannot mutate the snapshot."""
    for dirpath, dirnames, filenames in os.walk(root):
        for name in filenames:
            p = Path(dirpath) / name
            try:
                os.chmod(p, 0o444)
            except OSError:
                pass
        for name in dirnames:
            p = Path(dirpath) / name
            try:
                os.chmod(p, 0o555)
            except OSError:
                pass
    try:
        os.chmod(root, 0o555)
    except OSError:
        pass


def _rmtree_writable(path: Path) -> None:
    # The tree is chmod'd read-only; restore write bits so rmtree can remove it.
    for dirpath, dirnames, filenames in os.walk(path):
        for name in dirnames + filenames:
            try:
                os.chmod(Path(dirpath) / name, 0o700)
            except OSError:
                pass
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    shutil.rmtree(path, ignore_errors=True)


def snapshot_at_ref(
    ref: str,
    repo_root: str | None = None,
    *,
    max_bytes: int = DEFAULT_MAX_SNAPSHOT_BYTES,
) -> Path:
    """Return a read-only snapshot directory of the repo at ``ref`` (cached by SHA).

    Resolves ``ref`` to a SHA, and if ``.rebar/run_snapshots/<sha>`` already exists
    returns it (cache hit). Otherwise streams ``git archive <sha>`` through the
    hardened extractor into a temp dir, makes it read-only, and atomically renames
    it into place. Raises :class:`SnapshotError` on any failure (the partial temp
    dir is cleaned up). The caller never tears the snapshot down — the WS-C3 TTL
    sweep does — so re-runs at the same SHA are free.
    """
    sha = resolve_sha(ref, repo_root)
    root = str(repo_root) if repo_root else "."
    dest = _snapshot_root(repo_root) / sha
    if dest.is_dir():
        return dest  # cache hit (immutable by SHA)

    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(prefix=f".tmp-snap-{sha[:8]}-", dir=str(dest.parent)))
    proc = None
    try:
        proc = subprocess.Popen(
            ["git", "-C", root, "archive", "--format=tar", sha],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        # Stream the archive (mode "r|") so a large repo isn't buffered whole.
        with tarfile.open(fileobj=proc.stdout, mode="r|") as tar:
            tar.extractall(path=str(tmp), filter=_hardened_filter(max_bytes))
        _, stderr = proc.communicate()
        if proc.returncode != 0:
            raise SnapshotError(
                f"git archive {sha[:12]} failed: {stderr.decode('utf-8', 'replace').strip()}"
            )
        _chmod_readonly(tmp)
        # Atomic publish. If another run won the race, keep theirs and drop ours.
        try:
            os.rename(tmp, dest)
        except OSError:
            if dest.is_dir():
                _rmtree_writable(tmp)
                return dest
            raise
        return dest
    except BaseException:
        if proc is not None and proc.poll() is None:
            proc.kill()
        if tmp.exists():
            _rmtree_writable(tmp)
        raise
