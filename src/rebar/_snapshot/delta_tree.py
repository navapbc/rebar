"""Hardlink-donor delta materialization for the content-addressed snapshot store.

Why this exists
---------------
:func:`~rebar._snapshot.repo_snapshot.materialize_tickets` content-addresses its entry
as ``<store>/tickets-<sha>`` and builds it with a throwaway index +
``git checkout-index --all``. ``checkout-index`` writes the committed blob for EVERY tree
entry — no hardlink, no reflink, no delta against a neighbouring entry — so each build
costs a whole fresh copy of the tree.

The cache key is the LIVE tickets-branch tip, which advances roughly every 26 seconds
while each commit touches a handful of files. The cache-hit branch is therefore
effectively never taken, and the store grows by one full tree per gate resolution.
Measured in the field: 64,483 entries, 47.2 GiB for a ~620 MiB / ~70k-blob tree.

The fix, in one sentence: when the store already holds an entry for a NEIGHBOURING commit,
clone it with **hardlinks** (which cost inodes, not bytes) and then rewrite only the paths
that ``git diff`` says actually changed. N adjacent SHAs then consume ~ONE tree of distinct
on-disk bytes instead of N.

Mechanics that make this safe (each validated by experiment)
------------------------------------------------------------
* ``git checkout-index --force`` UNLINKS and recreates a path; it does not write *through*
  an existing hardlink. We nevertheless unlink every path we are about to rewrite
  ourselves, BEFORE git runs, so the "never mutate a published entry" guarantee is ours and
  does not depend on a git implementation detail.
* ``git checkout-index --force -z --stdin --prefix=<dir>/`` against a temp index that was
  ``read-tree``-d to the target sha writes exactly the paths fed on stdin.
* ``git diff --name-status -z <donor_sha> <sha>`` is the delta. ``D`` deletes from the
  build; anything else is a rewrite. Rename/copy statuses carry TWO path tokens, so both
  are consumed (we also pass ``--no-renames``, belt and braces).
* The donor's paths are enumerated from ``git ls-tree -r``, **never** by walking the donor
  directory. A live entry accumulates untracked files (a ``.cache.json`` per ticket dir,
  written by reads through the pinned root — thousands of them), which are absent from the
  committed tree; a directory-walk clone would copy them into the new entry and break the
  "byte-matches the tree" postcondition. See :func:`_link_tree_paths`.

Fail-closed policy
------------------
Faithfulness is the attestation basis (ADR 0005), so every doubt degrades to today's exact
full-materialize behaviour by returning ``False``: no donor found, donor objects absent,
``git diff``/``ls-tree`` failure, a donor that is INCOMPLETE (the janitor evicts by
rename-then-rmtree, so an entry can vanish mid-walk — we compare what we cloned against
``git ls-tree -r`` and discard on any mismatch), a tree containing entries we cannot
hardlink faithfully (symlinks, gitlinks), hardlinking unsupported (cross-device
``OSError``), or a delta that is not actually smaller than a full build.

One tracked path is deliberately never shared: see :data:`_UNSHAREABLE_BASENAMES`.
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

# Only plain blobs can be faithfully hardlinked: a symlink would be dereferenced by
# ``os.link`` (POSIX defaults to follow_symlinks=True) and a gitlink has no blob at all.
# A tree containing either falls back to a full materialization rather than risk drift.
_LINKABLE_MODES = frozenset({"100644", "100755"})

# Cap the donor search: the store can hold tens of thousands of entries and each candidate
# costs a ``git diff``. The newest entries are the ones adjacent to the sha we are building.
_MAX_DONOR_CANDIDATES = 8

_SHA_RE = re.compile(r"\A[0-9a-f]{40}\Z")

# Tracked files that must NEVER be hardlinked between entries, even though they are part of
# the committed tree. ``.ticket-write.lock`` is the store's advisory write lock, and
# ``fcntl.flock`` is scoped to the INODE — sharing one inode across two entries would make a
# lock taken through entry A block a writer in entry B. That coupling is invisible until it
# deadlocks, so these paths are written fresh into every entry (they are tiny).
_UNSHAREABLE_BASENAMES = frozenset({".ticket-write.lock"})


def _unshareable(paths: set[str]) -> set[str]:
    """The subset of ``paths`` that must be written fresh rather than hardlinked."""
    return {p for p in paths if os.path.basename(p) in _UNSHAREABLE_BASENAMES}


def _tree_paths(repo_root: str, sha: str) -> set[str] | None:
    """The blob paths of the committed tree at ``sha``, or ``None`` if it is not usable.

    ``None`` means "do not take the delta path": either git could not read the tree (the
    objects are absent from this clone) or the tree holds an entry we cannot hardlink
    faithfully (symlink / gitlink / anything not a plain blob)."""
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
    """Hardlink exactly ``paths`` from ``donor_tree`` into ``dest_tree``.

    ``paths`` comes from ``git ls-tree`` — NEVER from walking ``donor_tree``. A live entry
    holds untracked files as well as its committed tree: every ``show_ticket`` read through a
    pinned root drops a ``.cache.json`` (and transient ``..cache.json.*.tmp``) into the ticket
    dir — 4,844 of them measured inside ONE live entry. They are gitignored, so they are not
    in the tree. Cloning by directory walk would (a) copy those extras into the new entry,
    which then no longer byte-matches ``git ls-tree -r <sha>``, and (b) make any
    "walk-count == tree-count" completeness check fail forever in production, silently
    disabling the delta path while looking green on a clean fixture.

    Completeness is therefore verified path-by-path against the tree listing: a missing entry
    means the donor was partially evicted (the janitor renames then rmtree's), so we return
    ``False`` and the caller full-materializes. A symlink or a cross-device / unsupported-FS
    ``OSError`` is the same fail-closed answer."""
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
    # Bounded by the SAME timeout as the sibling ``read-tree`` above. ``git_run`` cannot
    # carry stdin, so the bound is applied here and a timeout is folded into the module's
    # fail-closed shape: log a diagnostic naming the operation (so a stall is
    # distinguishable from slow progress) and return False — the caller then falls back
    # to a full materialization.
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
    """Build ``sha``'s tree into ``dest_tree`` from a hardlinked neighbour, if possible.

    Returns ``True`` when ``dest_tree`` now byte-matches the committed tree at ``sha``, and
    ``False`` (leaving ``dest_tree`` cleaned out) when the caller must full-materialize —
    the fail-closed path for every doubt listed in the module docstring. ``entry_prefix`` +
    ``subdir`` describe the published layout to hunt donors in
    (``<store>/<entry_prefix><sha>/<subdir>/``)."""
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
