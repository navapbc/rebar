"""Build content-addressed snapshots from a hardlinked neighbouring tree.

Frequently advancing ticket tips rarely hit the exact-SHA cache. Reusing an adjacent
entry and rewriting only ``git diff`` paths avoids another full copy. Changed paths are
unlinked before a temporary-index ``checkout-index`` writes them, so published donor
inodes remain immutable. Donor paths come from ``git ls-tree``, never a directory walk
that would copy untracked derived files; rename/copy records are consumed safely even
though diffs disable rename detection. Lock files in :data:`_UNSHAREABLE_BASENAMES` are
always written fresh.

Faithfulness is the attestation basis, so every doubt returns ``False`` for full
materialization: no usable donor or objects, failed git plumbing, incomplete or mismatched
trees, symlinks/gitlinks, unsupported hardlinks, or a delta no smaller than the tree.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
from pathlib import Path

from rebar._snapshot.git_fetch import _GIT_TIMEOUT, git_run
from rebar._store.gitutil import run_git

_LOG = logging.getLogger(__name__)

# Only plain blobs are safe to hardlink; symlinks dereference and gitlinks lack blobs.
# Either kind makes the tree fall back to full materialization.
_LINKABLE_MODES = frozenset({"100644", "100755"})

# Search only the newest likely neighbours; each candidate costs a ``git diff``.
_MAX_DONOR_CANDIDATES = 8

_SHA_RE = re.compile(r"\A[0-9a-f]{40}\Z")

# Never share inode-scoped advisory lock files across entries: one entry's ``flock`` would
# otherwise block another. These tiny tracked paths are always rewritten.
_UNSHAREABLE_BASENAMES = frozenset({".ticket-write.lock"})


def _unshareable(paths: set[str]) -> set[str]:
    """The subset of ``paths`` that must be written fresh rather than hardlinked."""
    return {p for p in paths if os.path.basename(p) in _UNSHAREABLE_BASENAMES}


def _tree_paths(repo_root: str, sha: str) -> set[str] | None:
    """Return ``sha``'s plain-blob paths, or ``None`` when delta reuse is unsafe.

    Missing objects, unreadable trees, symlinks, and gitlinks fail closed."""
    proc = git_run(repo_root, "ls-tree", "-r", "-z", "--full-tree", "--end-of-options", sha)
    if proc.returncode != 0:
        return None
    paths: set[str] = set()
    for record in proc.stdout.split("\0"):
        if not record:
            continue
        meta, _, path = record.partition("\t")
        mode = meta.split(" ", 1)[0]
        if mode not in _LINKABLE_MODES or not path:
            return None
        paths.add(path)
    return paths


def _donor_candidates(store: Path, entry_prefix: str, sha: str) -> list[tuple[str, Path]]:
    """Published entries that could serve as a hardlink donor, newest first."""
    found: list[tuple[float, str, Path]] = []
    try:
        children = list(store.iterdir())
    except OSError:
        return []
    for child in children:
        name = child.name
        if not name.startswith(entry_prefix):
            continue
        candidate = name[len(entry_prefix) :]
        if candidate == sha or not _SHA_RE.match(candidate):
            continue
        try:
            mtime = child.stat().st_mtime
        except OSError:  # evicted mid-walk — just skip it
            continue
        found.append((mtime, candidate, child))
    found.sort(reverse=True)
    return [(c, p) for _m, c, p in found[:_MAX_DONOR_CANDIDATES]]


def _diff_paths(repo_root: str, donor_sha: str, sha: str) -> tuple[set[str], set[str]] | None:
    """``(deletes, writes)`` between two commits, or ``None`` if the diff is unusable."""
    proc = git_run(
        repo_root,
        "diff",
        "--name-status",
        "--no-renames",
        "-z",
        "--end-of-options",
        donor_sha,
        sha,
    )
    if proc.returncode != 0:
        return None
    tokens = [t for t in proc.stdout.split("\0") if t]
    deletes: set[str] = set()
    writes: set[str] = set()
    i = 0
    while i < len(tokens):
        status = tokens[i][:1]
        # Rename/copy statuses carry TWO path tokens; consume both (the destination is the
        # path that must be written, the source only disappears for a rename).
        width = 3 if status in ("R", "C") else 2
        if i + width > len(tokens):
            return None  # truncated record — never guess at a delta
        if width == 3:
            if status == "R":
                deletes.add(tokens[i + 1])
            writes.add(tokens[i + 2])
        elif status == "D":
            deletes.add(tokens[i + 1])
        else:
            writes.add(tokens[i + 1])
        i += width
    return deletes, writes


def _link_tree_paths(donor_tree: Path, dest_tree: Path, paths: set[str]) -> bool:
    """Hardlink exactly the ``git ls-tree`` paths from donor to destination.

    Never walking the donor excludes untracked caches and temporary files. A missing path
    signals partial eviction; symlinks, cross-device links, and unsupported filesystems are
    likewise unsafe. Each case returns ``False`` for full materialization."""
    dest_tree.mkdir(parents=True, exist_ok=True)
    made: set[Path] = set()
    for rel in paths:
        src = donor_tree / rel
        dst = dest_tree / rel
        parent = dst.parent
        if parent not in made:
            try:
                parent.mkdir(parents=True, exist_ok=True)
            except OSError:
                return False
            made.add(parent)
        if src.is_symlink():
            return False  # os.link would dereference it — never risk that
        try:
            os.link(src, dst)
        except OSError:
            # Missing (donor partially evicted / incomplete), cross-device, or an FS with
            # no hardlink support. All of them mean: do not trust this donor.
            return False
    return True


def _prune_empty_dirs(dest_tree: Path, rel_path: str) -> None:
    """Drop directories left empty by a delete, up to (but never including) ``dest_tree``."""
    parent = (dest_tree / rel_path).parent
    while parent != dest_tree and dest_tree in parent.parents:
        try:
            parent.rmdir()
        except OSError:
            return
        parent = parent.parent


def _apply_delta(
    repo_root: str, sha: str, dest_tree: Path, deletes: set[str], writes: set[str]
) -> bool:
    """Rewrite exactly ``writes`` and remove exactly ``deletes`` inside ``dest_tree``.

    Every path is unlinked BEFORE git writes it, so the donor's (published, immutable)
    inode is never written through — breaking the link is our guarantee, not git's."""
    for rel in deletes | writes:
        try:
            (dest_tree / rel).unlink()
        except FileNotFoundError:
            pass
        except OSError:
            return False
    for rel in deletes:
        _prune_empty_dirs(dest_tree, rel)
    if not writes:
        return True
    index_file = dest_tree.parent / (dest_tree.name + ".index")
    env = {**os.environ, "GIT_INDEX_FILE": str(index_file), "GIT_TERMINAL_PROMPT": "0"}
    read = git_run(repo_root, "read-tree", "--end-of-options", sha, env=env)
    if read.returncode != 0:
        return False
    payload = "\0".join(sorted(writes)) + "\0"
    # Share the read-tree timeout, but use run_git because checkout-index needs stdin.
    # A timeout is diagnosed and fails closed to full materialization.
    try:
        proc = run_git(
            repo_root,
            "checkout-index",
            "--force",
            "-z",
            "--stdin",
            f"--prefix={dest_tree}{os.sep}",
            check=False,
            env=env,
            input_data=payload,
            timeout=_GIT_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        _LOG.warning(
            "git checkout-index timed out after %ss writing %d path(s) into %s; "
            "falling back to full materialization",
            _GIT_TIMEOUT,
            len(writes),
            dest_tree,
        )
        return False
    return bool(proc.returncode == 0)


def _discard(dest_tree: Path) -> None:
    """Clear a half-built delta tree so the caller's full materialization starts clean."""
    shutil.rmtree(dest_tree, ignore_errors=True)
    try:
        (dest_tree.parent / (dest_tree.name + ".index")).unlink()
    except OSError:
        pass


def materialize_via_donor(
    repo_root: str,
    sha: str,
    dest_tree: Path,
    *,
    store: Path,
    entry_prefix: str,
    subdir: str,
) -> bool:
    """Build ``sha`` from a hardlinked neighbour in the published entry layout.

    Return ``True`` only for a byte-faithful tree; otherwise clean ``dest_tree`` and return
    ``False`` so the caller full-materializes."""
    target_paths = _tree_paths(repo_root, sha)
    if target_paths is None:
        return False
    for donor_sha, donor_entry in _donor_candidates(store, entry_prefix, sha):
        donor_paths = _tree_paths(repo_root, donor_sha)
        if donor_paths is None:
            continue
        delta = _diff_paths(repo_root, donor_sha, sha)
        if delta is None:
            continue
        deletes, writes = delta
        # Some tracked paths must never share an inode with another entry — see
        # _UNSHAREABLE_BASENAMES. Force them into the rewrite set so git writes them fresh.
        writes = writes | _unshareable(target_paths)
        # A delta no smaller than the tree itself buys nothing; prefer the simple path.
        if len(writes) >= len(donor_paths):
            continue
        # Link only the paths the delta leaves untouched; the rest git writes below.
        if _link_tree_paths(donor_entry / subdir, dest_tree, donor_paths - deletes - writes):
            if _apply_delta(repo_root, sha, dest_tree, deletes, writes):
                return True
        _discard(dest_tree)
    return False
